"""CcoAdapter — handles the cco stream-json protocol.

This is a direct extraction of the logic previously in
AgentRunner._handle_chunk(), with internal state (_current_msg,
_current_block_type) moved into the adapter instance.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from server.adapters.base import BaseAdapter, ChunkContext
from server.models import Message, TaskStatus

logger = logging.getLogger(__name__)


class CcoAdapter(BaseAdapter):
    def __init__(self) -> None:
        self._current_msg: Message | None = None
        self._current_block_type: str | None = None

    # ── build_args ──────────────────────────────────────────────────────

    def build_args(
        self,
        command: str,
        prompt: str,
        session_id: str | None,
        fork_sid: str | None,
        agent_cwd: str,
    ) -> list[str]:
        base_args = [
            command,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
        ]

        if fork_sid:
            return base_args + ["--resume", fork_sid, "--fork-session", prompt]
        elif session_id:
            return base_args + ["--resume", session_id, prompt]
        else:
            return base_args + [prompt]

    # ── handle_chunk ────────────────────────────────────────────────────

    async def handle_chunk(self, chunk: dict[str, Any], ctx: ChunkContext) -> None:
        chunk_type = chunk.get("type", "")

        if chunk_type == "system" and chunk.get("subtype") == "init":
            await self._handle_system_init(chunk, ctx)
            return

        if chunk_type == "stream_event":
            await self._handle_stream_event(chunk, ctx)
            return

        if chunk_type == "assistant":
            await self._handle_assistant(chunk, ctx)
            return

        if chunk_type == "user":
            await self._handle_user(chunk, ctx)
            return

        if chunk_type == "result":
            await self._handle_result(chunk, ctx)
            return

    # ── system init ─────────────────────────────────────────────────────

    async def _handle_system_init(self, chunk: dict, ctx: ChunkContext) -> None:
        sid = chunk.get("session_id")
        if sid:
            await ctx.save_session(sid)

    # ── stream_event ────────────────────────────────────────────────────

    async def _handle_stream_event(self, chunk: dict, ctx: ChunkContext) -> None:
        event = chunk.get("event", {})
        event_type = event.get("type", "")

        if event_type == "message_start":
            msg = await ctx.create_message("agent", "text", "", streaming=True)
            self._current_msg = msg
            self._current_block_type = None

        elif event_type == "content_block_start":
            content_block = event.get("content_block", {})
            block_type = content_block.get("type", "text")
            self._current_block_type = block_type

            if block_type == "tool_use":
                tool_name = content_block.get("name", "")
                # Close current text message if streaming
                cur = self._current_msg
                if cur and cur.streaming and cur.type == "text":
                    await ctx.close_message(cur.id)
                    cur.streaming = False

                msg = await ctx.create_message(
                    "agent", "tool_use", "",
                    tool_name=tool_name, streaming=True,
                )
                self._current_msg = msg

            elif block_type == "text":
                cur = self._current_msg
                if not cur or cur.type != "text" or not cur.streaming:
                    msg = await ctx.create_message("agent", "text", "", streaming=True)
                    self._current_msg = msg

        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            delta_type = delta.get("type", "")
            msg = self._current_msg

            if delta_type == "text_delta":
                text = delta.get("text", "")
                if msg and text:
                    msg.content += text
                    await ctx.append_delta(msg.id, text)

            elif delta_type == "input_json_delta":
                partial = delta.get("partial_json", "")
                if msg and partial and msg.type == "tool_use":
                    msg.content += partial
                    await ctx.append_delta(msg.id, partial)

        elif event_type == "content_block_stop":
            msg = self._current_msg
            if msg:
                msg.streaming = False
                await ctx.close_message(msg.id)
                self._current_msg = None
            self._current_block_type = None

    # ── assistant ───────────────────────────────────────────────────────

    async def _handle_assistant(self, chunk: dict, ctx: ChunkContext) -> None:
        from server.state import app_state

        task = app_state.get_task(ctx.task_id)
        if not task:
            return

        message_data = chunk.get("message", {})
        content_blocks = message_data.get("content", [])

        logger.info(
            "assistant chunk for task %s: %d content blocks",
            ctx.task_id, len(content_blocks),
        )

        # Close any still-streaming message
        cur = self._current_msg
        if cur and cur.streaming:
            cur.streaming = False
            await ctx.close_message(cur.id)
        self._current_msg = None

        # Fix up server-side message content from authoritative assistant chunk
        text_msgs = [m for m in task.messages if m.role == "agent" and m.type == "text"]
        tool_msgs = [m for m in task.messages if m.role == "agent" and m.type == "tool_use"]
        text_idx = 0
        tool_idx = 0

        for block in content_blocks:
            btype = block.get("type", "")
            if btype == "text":
                full_text = block.get("text", "")
                if not full_text:
                    continue
                if text_idx < len(text_msgs):
                    text_msgs[text_idx].content = full_text
                    text_msgs[text_idx].streaming = False
                text_idx += 1
            elif btype == "tool_use":
                tool_input = block.get("input", {})
                content_str = json.dumps(tool_input, ensure_ascii=False)
                if tool_idx < len(tool_msgs):
                    tool_msgs[tool_idx].content = content_str
                    tool_msgs[tool_idx].streaming = False
                tool_idx += 1

        # Per-turn token usage
        turn_model = message_data.get("model", "")
        turn_usage = message_data.get("usage", {})
        if turn_model and turn_usage:
            in_tok = (
                turn_usage.get("input_tokens", 0)
                + turn_usage.get("cache_read_input_tokens", 0)
                + turn_usage.get("cache_creation_input_tokens", 0)
            )
            out_tok = turn_usage.get("output_tokens", 0)
            ctx_win = turn_usage.get("context_window", 0)
            await ctx.update_tokens(in_tok, out_tok, model=turn_model, context_window=ctx_win)

        app_state.save_agent_tasks(task.agent_id)

    # ── user (tool_result) ──────────────────────────────────────────────

    async def _handle_user(self, chunk: dict, ctx: ChunkContext) -> None:
        message_data = chunk.get("message", {})
        content_blocks = message_data.get("content", [])
        for block in content_blocks:
            if block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    parts = []
                    for part in result_content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            parts.append(part.get("text", ""))
                    result_content = "\n".join(parts)
                if not isinstance(result_content, str):
                    result_content = json.dumps(result_content, ensure_ascii=False)

                await ctx.create_message(
                    "agent", "tool_result", result_content, streaming=False,
                )

    # ── result ──────────────────────────────────────────────────────────

    async def _handle_result(self, chunk: dict, ctx: ChunkContext) -> None:
        from server.state import app_state

        logger.info(
            "result chunk for task %s: %s",
            ctx.task_id, json.dumps(chunk, ensure_ascii=False)[:2000],
        )

        subtype = chunk.get("subtype", "")
        is_error = chunk.get("is_error", False)
        num_turns = chunk.get("num_turns", 0)
        cost_usd = chunk.get("total_cost_usd", 0) or 0
        result_text = chunk.get("result", "")
        errors = chunk.get("errors", []) if is_error or subtype in ("error", "error_during_execution") else None

        # Apply authoritative modelUsage from result chunk (session totals)
        model_usage = chunk.get("modelUsage", {})
        if model_usage:
            await ctx.apply_authoritative_usage(model_usage)
        else:
            # Fallback: no per-model data — use legacy usage block
            task = app_state.get_task(ctx.task_id)
            if task and not task.total_input_tokens:
                usage = chunk.get("usage", {})
                task.total_input_tokens = (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                )
                task.total_output_tokens = usage.get("output_tokens", 0)

        await ctx.finish(
            status=TaskStatus.failed if (is_error or subtype in ("error", "error_during_execution")) else TaskStatus.success,
            num_turns=num_turns,
            cost_usd=cost_usd,
            result_text=result_text if isinstance(result_text, str) else "",
            errors=[str(e) for e in errors] if errors else None,
        )

    # ── cleanup ─────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear internal state between sessions."""
        self._current_msg = None
        self._current_block_type = None
