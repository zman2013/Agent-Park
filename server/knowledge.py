"""Project-signal extraction and the read-only knowledge archive.

What is left here after auto-memory took over consolidation:

- ``extract_project_signals`` — signal source for the project layer.
- ``_llm_call`` — the read-only helper-LLM invocation, shared with auto-memory.
- ``read_knowledge_docs`` — ``data/knowledge/{eid}/`` is kept in place as a
  read-only archive so the migration stays reversible.

Removed earlier: ``merge_errors`` / ``merge_project`` (whole-document free-text
rewrite, measured destroying real documents), ``extract_error_signals`` (any
5-200 char user message counted as a "correction"), ``build_memory_entries`` /
``update_memory_index`` (lossy one-line derivatives), and
``_AGENT_PARK_NOISE_KEYWORDS`` — that last guard filtered *inputs* that
mentioned agent-park symbols, which is self-destructive for agent-park's own
agent, and its companion prompt clause ("if the conversation is about
agent-park internals, return the existing document unchanged") caused the
pollution it meant to prevent: the LLM complied by narrating that it was
returning the document unchanged, and the narration became the document.

Removed with this change: ``compute_hotfiles`` / ``build_hotfiles_md`` and
their five path helpers. The hotfiles layer was the only one that never called
an LLM and never got polluted — but a ranked table of file-access counts told
the model *which* files were touched and never *what happened to them*, so it
spent 4000 characters of the injection budget on something unactionable. The
history layer replaced it: same zero-LLM property, but it records outcomes.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"


# ── Directory helpers ──────────────────────────────────────────────────────────

def knowledge_dir(agent_id: str) -> Path:
    from server.auto_memory import effective_id
    eid = effective_id(agent_id)
    return KNOWLEDGE_DIR / eid


# ── Signal extraction (no LLM) ────────────────────────────────────────────────

def extract_project_signals(tasks: list) -> list[dict]:
    """Extract project knowledge fragments from agent text messages."""
    signals = []
    for task in tasks:
        messages = task.messages if hasattr(task, "messages") else []
        # Only keep the first agent text per task to avoid repetition
        agent_texts_seen = 0
        for msg in messages:
            role = getattr(msg, "role", "")
            msg_type = getattr(msg, "type", "")
            content = getattr(msg, "content", "")

            # agent text describing project structure / commands
            if role == "agent" and msg_type == "text" and len(content) > 30:
                # heuristic: contains path separators or command keywords
                if any(kw in content for kw in ["/", "python ", "pytest", "目录", "文件", "路径", "命令", "command", "script"]):
                    if agent_texts_seen < 3:  # limit agent texts per task
                        signals.append({
                            "source": "agent_text",
                            "content": content[:800],
                            "task_id": task.id,
                            "task_name": task.name,
                        })
                        agent_texts_seen += 1

            # user providing facts
            if role == "user" and msg_type == "text" and len(content) > 10:
                signals.append({
                    "source": "user_text",
                    "content": content[:600],
                    "task_id": task.id,
                    "task_name": task.name,
                })
    return signals


def _read_existing(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ── LLM merge ─────────────────────────────────────────────────────────────────

async def _llm_call(command: str, prompt: str, timeout: int = 120) -> str:
    """Call an LLM command with -p flag and stream-json output, return result text.

    Denies the write tools. These commands are full coding agents launched with
    ``--dangerously-skip-permissions``, and we only ever read their stdout — but
    left unrestricted one was observed rewriting ``docs/error_experience.md``:
    asked to *return* a merged ``errors.md``, it found a repo doc with the same
    heading format, decided that was the intended target, and edited it.

    ``cwd`` alone does not contain this (the agent uses absolute paths), and
    ``--disallowed-tools`` is variadic and swallows the trailing prompt argument.
    A ``--settings`` deny list is the combination that blocks the write tools
    while leaving the text result intact.
    """
    from server.agent_runner import _clean_env
    from server.helper_llm import READONLY_SETTINGS
    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--settings", READONLY_SETTINGS,
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # Without this the child inherits EPT_CLAUDE_RUNNING whenever the
            # server itself runs under `ept claude`; the wrapper reads that as a
            # nested launch, prints usage to stderr and exits 1. stderr is
            # discarded, so the failure surfaces only as an empty result — and
            # every caller treats empty as "nothing to change", turning the whole
            # pipeline into a silent no-op.
            env=_clean_env(),
        )
    except Exception:
        logger.exception("LLM call failed to start (command=%s)", command)
        return ""

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        from server.helper_llm import parse_stream_json_result
        result = parse_stream_json_result(stdout.decode("utf-8", errors="replace"))
        return result.strip() if result else ""
    except asyncio.TimeoutError:
        logger.warning("LLM call timed out (command=%s)", command)
        await _kill(proc, command)
        return ""
    except asyncio.CancelledError:
        # Shutdown. Cancelling communicate() does NOT signal the child: the
        # bounded consolidation drain in AgentRunner.shutdown is 5s while this
        # timeout is 600s, and run.sh only signals the backend PID — so a helper
        # still thinking would be orphaned, left running and billing for up to
        # the full call duration. Kill it, then let the cancellation propagate.
        logger.info("LLM call cancelled, killing helper (command=%s)", command)
        await _kill(proc, command)
        raise
    except Exception:
        logger.exception("LLM call failed (command=%s)", command)
        await _kill(proc, command)
        return ""


async def _kill(proc, command: str) -> None:
    """Terminate *proc* and reap it, so it cannot outlive this call.

    Reaping matters as much as signalling: an un-awaited child becomes a zombie,
    and ``run.sh``'s ``is_running`` treats a zombie backend PID as alive.
    ``asyncio.shield`` because this runs on the cancellation path, where a bare
    await would be cancelled again before the wait completed.
    """
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return                                  # exited between the check and here
    except Exception:
        logger.exception("Failed to kill helper LLM (command=%s)", command)
        return
    try:
        await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=5))
    except Exception:
        logger.warning("Helper LLM did not reap after kill (command=%s)", command)


# ── Write documents ───────────────────────────────────────────────────────────

def read_knowledge_docs(agent_id: str) -> dict[str, str]:
    """Read the legacy archive. Nothing writes these documents any more."""
    kdir = knowledge_dir(agent_id)
    return {
        "errors": _read_existing(kdir / "errors.md"),
        "project": _read_existing(kdir / "project.md"),
        "hotfiles": _read_existing(kdir / "hotfiles.md"),
    }


