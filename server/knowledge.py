"""Hotfile statistics and the read-only knowledge archive.

What is left here after auto-memory took over consolidation:

- ``compute_hotfiles`` / ``build_hotfiles_md`` — pure-Python file-heat stats.
  Across two independent measurements this was the only layer never polluted by
  meta-narration, and it is the only one that never called an LLM. That
  correlation is why it is reused verbatim.
- ``extract_project_signals`` — signal source for the project layer.
- ``_llm_call`` — the read-only helper-LLM invocation, shared with auto-memory.
- ``read_knowledge_docs`` / ``write_knowledge_docs`` — ``data/knowledge/{eid}/``
  is kept in place as a read-only archive so the migration stays reversible.

Removed with this change: ``merge_errors`` / ``merge_project`` (whole-document
free-text rewrite, measured destroying real documents), ``extract_error_signals``
(any 5-200 char user message counted as a "correction"),
``build_memory_entries`` / ``update_memory_index`` (lossy one-line derivatives),
and ``_AGENT_PARK_NOISE_KEYWORDS`` — that last guard filtered *inputs* that
mentioned agent-park symbols, which is self-destructive for agent-park's own
agent, and its companion prompt clause ("if the conversation is about
agent-park internals, return the existing document unchanged") caused the
pollution it meant to prevent: the LLM complied by narrating that it was
returning the document unchanged, and the narration became the document.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
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


_HOTFILE_EXCLUDE_PREFIXES = (
    "/tmp/",
    "/var/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/run/",
)


def _is_under_root(fp: str, root: str) -> bool:
    """Return True if fp equals root or is nested under root (prefix-safe)."""
    root_clean = root.rstrip("/")
    return fp == root_clean or fp.startswith(f"{root_clean}/")


def _is_project_file(fp: str, project_roots: tuple[str, ...] = ()) -> bool:
    """Return True if the file path is a meaningful project file (not temp/system noise)."""
    fp = fp.strip()
    if not fp:
        return False

    for prefix in _HOTFILE_EXCLUDE_PREFIXES:
        if fp.startswith(prefix):
            # Keep files inside known project roots even if repo is under /tmp, /var, etc.
            if project_roots and any(_is_under_root(fp, root) for root in project_roots):
                return True
            return False
    return True


def compute_hotfiles(tasks: list, recent_days: int = 7, project_root: str | None = None) -> list[dict]:
    """Count file access frequency from tool_use messages (no LLM)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    read_counts: dict[str, int] = defaultdict(int)
    edit_counts: dict[str, int] = defaultdict(int)
    last_access: dict[str, str] = {}
    project_roots = tuple([project_root.rstrip("/")]) if project_root else ()

    for task in tasks:
        # check task updated_at for recency
        updated_at = getattr(task, "updated_at", "")
        try:
            ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if ts < cutoff:
                continue
        except Exception:
            pass

        messages = task.messages if hasattr(task, "messages") else []
        for msg in messages:
            role = getattr(msg, "role", "")
            msg_type = getattr(msg, "type", "")
            tool_name = getattr(msg, "tool_name", "")
            content = getattr(msg, "content", "")

            if role != "agent" or msg_type != "tool_use":
                continue

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if tool_name in ("Read",):
                # content is JSON with file_path
                fp = _extract_file_path(content, tool_name)
                if fp and _is_project_file(fp, project_roots):
                    read_counts[fp] += 1
                    last_access[fp] = today

            elif tool_name in ("Edit", "Write", "NotebookEdit"):
                fp = _extract_file_path(content, tool_name)
                if fp and _is_project_file(fp, project_roots):
                    edit_counts[fp] += 1
                    last_access[fp] = today

            elif tool_name == "Bash":
                fps = _extract_paths_from_bash(content)
                for fp in fps:
                    if _is_project_file(fp, project_roots):
                        read_counts[fp] += 1
                        last_access[fp] = today

    # merge and sort
    all_files = set(list(read_counts.keys()) + list(edit_counts.keys()))
    result = []
    for fp in all_files:
        r = read_counts.get(fp, 0)
        e = edit_counts.get(fp, 0)
        weight = r + e * 2  # edits count more
        result.append({
            "file": fp,
            "reads": r,
            "edits": e,
            "weight": weight,
            "last_access": last_access.get(fp, ""),
        })
    result.sort(key=lambda x: -x["weight"])
    return result


def _extract_file_path(content: str, tool_name: str) -> str | None:
    """Try to extract file_path from tool_use content (JSON or plain text)."""
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            for key in ("file_path", "notebook_path", "path"):
                if key in obj:
                    return str(obj[key])
    except Exception:
        pass
    # fallback: regex for absolute paths
    m = re.search(r'["\']?(/[\w/.\-_]+\.\w+)["\']?', content)
    if m:
        return m.group(1)
    return None


def _extract_paths_from_bash(content: str) -> list[str]:
    """Extract file paths from bash command content."""
    paths = []
    try:
        obj = json.loads(content)
        cmd = obj.get("command", "") if isinstance(obj, dict) else ""
    except Exception:
        cmd = content
    for m in re.finditer(r'(/[\w/.\-_]+\.(?:py|cpp|h|mlir|json|yaml|yml|md|sh|txt))', cmd):
        paths.append(m.group(1))
    return paths


# ── Markdown document builders ────────────────────────────────────────────────

def _read_existing(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def build_hotfiles_md(hotfiles: list[dict], max_items: int = 20) -> str:
    top = hotfiles[:max_items]
    if not top:
        return "## 热点文件（最近 7 天）\n\n暂无数据\n"
    lines = ["## 热点文件（最近 7 天）\n", "| 文件 | 读取 | 编辑 | 最近访问 |", "|------|------|------|----------|"]
    for f in top:
        lines.append(f"| {f['file']} | {f['reads']} | {f['edits']} | {f['last_access']} |")
    return "\n".join(lines) + "\n"


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
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        from server.helper_llm import parse_stream_json_result
        result = parse_stream_json_result(stdout.decode("utf-8", errors="replace"))
        return result.strip() if result else ""
    except asyncio.TimeoutError:
        logger.warning("LLM call timed out (command=%s)", command)
        return ""
    except Exception:
        logger.exception("LLM call failed (command=%s)", command)
        return ""


# ── Write documents ───────────────────────────────────────────────────────────

def read_knowledge_docs(agent_id: str) -> dict[str, str]:
    """Read the legacy archive. Nothing writes these documents any more."""
    kdir = knowledge_dir(agent_id)
    return {
        "errors": _read_existing(kdir / "errors.md"),
        "project": _read_existing(kdir / "project.md"),
        "hotfiles": _read_existing(kdir / "hotfiles.md"),
    }


