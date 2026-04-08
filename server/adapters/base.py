"""Base adapter and chunk context protocol for agent subprocess protocols."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol

from server.models import Message, TaskStatus


class ChunkContext(Protocol):
    """Callback interface provided by AgentRunner to adapters."""

    task_id: str

    async def save_session(self, session_id: str) -> None: ...

    async def on_session_renewed(self, old_sid: str, new_sid: str) -> None: ...

    async def create_message(
        self,
        role: str,
        type_: str,
        content: str,
        tool_name: str = "",
        streaming: bool = False,
    ) -> Message: ...

    async def append_delta(self, message_id: str, text: str) -> None: ...

    async def close_message(self, message_id: str) -> None: ...

    async def send_system_notice(self, content: str) -> None: ...

    async def update_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str = "",
        context_window: int = 0,
    ) -> None: ...

    async def apply_authoritative_usage(
        self,
        model_usage: dict[str, dict],
    ) -> None:
        """Apply authoritative session-total token usage from a result chunk.

        This merges session totals onto the baseline snapshot captured at
        session start, replacing any per-turn incremental updates.
        Used by cco's result chunk which carries definitive modelUsage.
        """
        ...

    async def finish(
        self,
        status: TaskStatus,
        num_turns: int = 0,
        cost_usd: float = 0,
        result_text: str = "",
        errors: list[str] | None = None,
    ) -> None: ...


class BaseAdapter(ABC):
    """Abstract base for protocol-specific adapters."""

    @abstractmethod
    def build_args(
        self,
        command: str,
        prompt: str,
        session_id: str | None,
        fork_sid: str | None,
        agent_cwd: str,
    ) -> list[str]:
        """Build subprocess command-line arguments."""
        ...

    @abstractmethod
    async def handle_chunk(self, chunk: dict[str, Any], ctx: ChunkContext) -> None:
        """Process one parsed JSON chunk from the subprocess output."""
        ...

    def needs_pty(self) -> bool:
        """Whether the subprocess requires a PTY (default True for cco)."""
        return True
