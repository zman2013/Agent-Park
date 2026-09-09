"""Shared plumbing for helper-LLM subprocess calls.

These three pieces lived in ``memory.py`` for historical reasons — they are not
part of the memory system, they are what every non-agent LLM invocation needs:
knowledge extraction, wiki ingest, and profile-entry compression all call out to
a coding-agent CLI and read one text result back.

``READONLY_SETTINGS`` is the important one. These commands are full coding agents
launched with ``--dangerously-skip-permissions`` and we only ever read their
stdout — but left unrestricted one was observed rewriting
``docs/error_experience.md``: asked to *return* a merged document, it found a repo
file with the same heading format, decided that was the intended target, and
edited it. ``cwd`` does not contain this (the agent uses absolute paths), and
``--disallowed-tools`` is variadic, swallowing the trailing prompt argument. A
``--settings`` deny list is the combination that blocks the write tools while
leaving the text result intact.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 300  # characters; reject if a compressed result exceeds this

READONLY_SETTINGS = json.dumps({
    "permissions": {
        "deny": ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash",
                 "Task", "WebFetch", "WebSearch"],
    }
})


def parse_stream_json_result(output: str) -> str:
    """Extract the final text content from stream-json output."""
    text_parts: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        obj_type = obj.get("type")
        # Prefer the result event's text (final assistant message)
        if obj_type == "result":
            result_text = obj.get("result", "")
            if result_text:
                return result_text
        # Accumulate text deltas as fallback
        if obj_type == "stream_event":
            event = obj.get("event", {})
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_parts.append(delta.get("text", ""))
    return "".join(text_parts)


async def compress_content(content: str, command: str) -> str:
    """Compress *content* into one line using the given LLM command.

    Falls back to returning *content* unchanged on any error: this is a
    convenience for the profile editor, and refusing to save the user's own text
    because a helper LLM was unavailable would be worse than saving it verbatim.
    """
    compress_prompt = (
        "请将以下内容压缩为一条简洁的记录，去除冗余信息，保留关键事实，"
        "用中文输出（仅输出压缩后的内容，不要任何解释或前缀）：\n"
        + content
    )
    try:
        from server.agent_runner import _clean_env
        proc = await asyncio.create_subprocess_exec(
            command,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--settings", READONLY_SETTINGS,
            compress_prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # See _clean_env: the child must not inherit EPT_CLAUDE_RUNNING, or
            # the wrapper treats it as a nested launch and exits 1 with no
            # stdout, which here silently degrades to "return content unchanged".
            env=_clean_env(),
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        result_text = parse_stream_json_result(stdout.decode("utf-8", errors="replace"))
        if result_text:
            return result_text.strip()
    except asyncio.TimeoutError:
        logger.warning("compress_content timed out for command %s", command)
    except Exception:
        logger.exception("compress_content failed for command %s", command)
    return content
