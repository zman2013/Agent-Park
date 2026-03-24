"""Per-agent memory stored as JSONL files.

Each line: {"type": "note", "timestamp": "...", "content": "..."}
Files stored at: data/memory/{agent_id}.jsonl
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MEMORY_DIR = DATA_DIR / "memory"

MAX_CONTENT_LENGTH = 300  # characters; reject if compressed result exceeds this


def effective_memory_agent_id(agent_id: str) -> str:
    """Return the agent id whose memory file should be used.

    If the agent has shared_memory_agent_id set, reads/writes go to that
    agent's memory file instead of its own.
    """
    from server.state import app_state
    agent = app_state.get_agent(agent_id)
    if agent and agent.shared_memory_agent_id:
        return agent.shared_memory_agent_id
    return agent_id


def _memory_path(agent_id: str) -> Path:
    return MEMORY_DIR / f"{agent_id}.jsonl"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_memory(agent_id: str, max_lines: int = 200) -> list[str]:
    """Return the last *max_lines* memory entries as plain content strings."""
    path = _memory_path(effective_memory_agent_id(agent_id))
    if not path.exists():
        return []
    try:
        raw_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        recent = raw_lines[-max_lines:]
        result = []
        for line in recent:
            try:
                entry = json.loads(line)
                result.append(entry.get("content", ""))
            except json.JSONDecodeError:
                continue
        return result
    except Exception:
        logger.exception("Failed to load memory for agent %s", agent_id)
        return []


def list_memory(agent_id: str) -> list[dict]:
    """Return all memory entries newest-first, each with an added `line_index` field."""
    path = _memory_path(effective_memory_agent_id(agent_id))
    if not path.exists():
        return []
    try:
        raw_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        entries = []
        for idx, line in enumerate(raw_lines):
            try:
                entry = json.loads(line)
                entry["line_index"] = idx
                entries.append(entry)
            except json.JSONDecodeError:
                continue
        entries.reverse()
        return entries
    except Exception:
        logger.exception("Failed to list memory for agent %s", agent_id)
        return []


def append_memory(agent_id: str, entry: dict) -> None:
    """Append a single entry to the agent's JSONL file."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path = _memory_path(effective_memory_agent_id(agent_id))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def delete_memory_line(agent_id: str, line_index: int) -> bool:
    """Remove the entry at *line_index* by rewriting the file. Returns True on success."""
    path = _memory_path(effective_memory_agent_id(agent_id))
    if not path.exists():
        return False
    try:
        raw_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if line_index < 0 or line_index >= len(raw_lines):
            return False
        del raw_lines[line_index]
        path.write_text("\n".join(raw_lines) + ("\n" if raw_lines else ""), encoding="utf-8")
        return True
    except Exception:
        logger.exception("Failed to delete memory line %d for agent %s", line_index, agent_id)
        return False


async def compress_content(content: str, command: str) -> str:
    """Compress *content* using the given LLM command.

    Runs: {command} -p --output-format stream-json <prompt>
    Parses stream-json to extract the final text result.
    Falls back to returning the original *content* on any error.
    """
    compress_prompt = (
        "请将以下内容压缩为一条简洁的记录，去除冗余信息，保留关键事实，"
        "用中文输出（仅输出压缩后的内容，不要任何解释或前缀）：\n"
        + content
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            compress_prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        result_text = _parse_stream_json_result(stdout.decode("utf-8", errors="replace"))
        if result_text:
            return result_text.strip()
    except asyncio.TimeoutError:
        logger.warning("compress_content timed out for command %s", command)
    except Exception:
        logger.exception("compress_content failed for command %s", command)
    return content


def _parse_stream_json_result(output: str) -> str:
    """Extract the final text content from cco stream-json output."""
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
