"""CodexAdapter — handles the codex exec --json JSONL protocol.

codex JSONL events:
  {"type":"thread.started","thread_id":"..."}
  {"type":"turn.started"}
  {"type":"item.started","item":{"type":"command_execution","command":"..."}}
  {"type":"item.completed","item":{"type":"command_execution","command":"...","aggregated_output":"..."}}
  {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
  {"type":"turn.completed","usage":{"input_tokens":N,"output_tokens":N}}
  (process exits — no explicit result chunk)
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

from server.adapters.base import BaseAdapter, ChunkContext
from server.models import Message, TaskStatus

logger = logging.getLogger(__name__)


class CodexAdapter(BaseAdapter):
    def __init__(self) -> None:
        # Track the currently streaming tool_use message for command_execution
        self._current_tool_msg: Message | None = None

    # ── build_args ──────────────────────────────────────────────────────

    def build_args(
        self,
        command: str,
        prompt: str,
        session_id: str | None,
        fork_sid: str | None,
        agent_cwd: str,
    ) -> list[str]:
        # command might be "ept codex", "ept codex -m k2", etc.
        parts = shlex.split(command)

        common = ["--json", "--dangerously-bypass-approvals-and-sandbox"]
        if agent_cwd:
            common += ["-C", agent_cwd]

        if session_id:
            # resume: -C is not accepted by `exec resume`, omit it
            resume_flags = ["--json", "--dangerously-bypass-approvals-and-sandbox"]
            return parts + ["exec", "resume"] + resume_flags + [session_id, prompt]
        else:
            # new session: ept codex exec --json ... PROMPT
            return parts + ["exec"] + common + [prompt]

    # ── needs_pty ───────────────────────────────────────────────────────

    def needs_pty(self) -> bool:
        return False  # codex exec --json outputs pure JSONL, no PTY needed

    # ── handle_chunk ────────────────────────────────────────────────────

    async def handle_chunk(self, chunk: dict[str, Any], ctx: ChunkContext) -> None:
        chunk_type = chunk.get("type", "")

        if chunk_type == "thread.started":
            thread_id = chunk.get("thread_id", "")
            if thread_id:
                await ctx.save_session(thread_id)
            return

        if chunk_type == "turn.started":
            # No action needed
            return

        if chunk_type == "item.started":
            await self._handle_item_started(chunk, ctx)
            return

        if chunk_type == "item.completed":
            await self._handle_item_completed(chunk, ctx)
            return

        if chunk_type == "turn.completed":
            await self._handle_turn_completed(chunk, ctx)
            return

    # ── item.started ────────────────────────────────────────────────────

    async def _handle_item_started(self, chunk: dict, ctx: ChunkContext) -> None:
        item = chunk.get("item", {})
        item_type = item.get("type", "")

        if item_type == "command_execution":
            command = item.get("command", "")
            msg = await ctx.create_message(
                "agent", "tool_use", command,
                tool_name="Bash", streaming=True,
            )
            self._current_tool_msg = msg

    # ── item.completed ──────────────────────────────────────────────────

    async def _handle_item_completed(self, chunk: dict, ctx: ChunkContext) -> None:
        item = chunk.get("item", {})
        item_type = item.get("type", "")

        if item_type == "command_execution":
            command = item.get("command", "")
            output = item.get("aggregated_output", "")

            # Close the tool_use message
            cur = self._current_tool_msg
            if cur:
                # Update content to the command (may already be set)
                cur.content = command
                cur.streaming = False
                await ctx.close_message(cur.id)
                self._current_tool_msg = None

            # Send tool_result with the output
            await ctx.create_message(
                "agent", "tool_result", output, streaming=False,
            )

        elif item_type == "agent_message":
            text = item.get("text", "")
            if text:
                await ctx.create_message(
                    "agent", "text", text, streaming=False,
                )

    # ── turn.completed ──────────────────────────────────────────────────

    async def _handle_turn_completed(self, chunk: dict, ctx: ChunkContext) -> None:
        usage = chunk.get("usage", {})
        if usage:
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            model = usage.get("model", "")
            await ctx.update_tokens(in_tok, out_tok, model=model)

    # ── cleanup ─────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear internal state between sessions."""
        self._current_tool_msg = None
