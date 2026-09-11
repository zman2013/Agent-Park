from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


def _uid() -> str:
    return uuid.uuid4().hex[:12]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStatus(str, Enum):
    idle = "idle"
    running = "running"
    waiting = "waiting"
    success = "success"
    failed = "failed"


class Message(BaseModel):
    id: str = Field(default_factory=_uid)
    role: Literal["agent", "user"] = "agent"
    type: Literal["text", "tool_use", "tool_result", "system"] = "text"
    content: str = ""
    tool_name: str = ""
    streaming: bool = False
    # Per-turn LLM usage at the moment this message was finalized.
    # Only set on the LAST agent message of a turn. Shape:
    #   {"input_tokens": int, "output_tokens": int,
    #    "context_window": int, "model": str}
    # context_window may be 0 when the protocol does not expose it (codex).
    usage: dict | None = None
    # CCo native message uuid (from stream-json assistant chunk).
    # Used with --resume-session-at to fork from this message.
    cco_uuid: str = ""
    # When this message was created. Messages persisted before this field
    # existed are backfilled with "" on load rather than with the restart
    # time, so the UI shows nothing instead of a fabricated moment.
    created_at: str = Field(default_factory=_utcnow)


class Task(BaseModel):
    id: str = Field(default_factory=_uid)
    agent_id: str
    name: str = ""
    prompt: str = ""
    status: TaskStatus = TaskStatus.idle
    messages: list[Message] = Field(default_factory=list)
    num_turns: int = 0
    # Cumulative token usage across all turns
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    context_window: int = 0
    total_cost_cny: float = 0.0
    # Per-model cumulative usage: {model_name: {inputTokens, outputTokens, contextWindow}}
    model_usage: dict = Field(default_factory=dict)
    updated_at: str = Field(default_factory=_utcnow)
    # Set when this task was forked from another session; consumed on first message
    fork_session_id: str | None = None
    # CCo message uuid to truncate history at when forking; consumed on first message
    fork_resume_at: str | None = None
    # How many messages this task inherited from its fork source. A fork gets a
    # new id but a deep copy of the source's transcript, so auto-memory's
    # per-task watermark would see that whole copy as new and re-feed material it
    # already consolidated under the source id. Seeds the watermark instead.
    inherited_messages: int = 0


class Agent(BaseModel):
    id: str = Field(default_factory=_uid)
    name: str
    command: str = "cco"
    cwd: str = ""
    wiki: str = ""  # wiki directory name, resolved to <wiki_ingest.wiki_base>/<name>/
    task_ids: list[str] = Field(default_factory=list)
    shared_memory_agent_id: str | None = None
    pinned: bool = False
    archived: bool = False
