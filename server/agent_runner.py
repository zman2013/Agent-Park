"""Agent subprocess manager – spawns cco and manages I/O.

cco stream-json protocol (with --include-partial-messages):
  {"type":"system","subtype":"init", ...}
  {"type":"stream_event","event":{"type":"message_start", ...}}
  {"type":"stream_event","event":{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}}
  {"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}}
  {"type":"stream_event","event":{"type":"content_block_stop","index":0}}
  {"type":"assistant","message":{"content":[{"type":"text","text":"Hello!"}], ...}}
  {"type":"stream_event","event":{"type":"message_delta","delta":{"stop_reason":"end_turn"}, ...}}
  {"type":"stream_event","event":{"type":"message_stop", ...}}
  {"type":"result","subtype":"success"|"error", ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from server.models import Message, TaskStatus
from server.state import app_state

logger = logging.getLogger(__name__)


class AgentRunner:
    def __init__(self) -> None:
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._current_msg: dict[str, Message] = {}
        self._session_ids: dict[str, str] = {}  # task_id -> cco session_id

    async def run_task(self, task_id: str, prompt: str) -> None:
        """Spawn an agent subprocess and stream output."""
        task = app_state.get_task(task_id)
        if not task:
            return

        task.status = TaskStatus.running
        await self._broadcast_status(task_id, TaskStatus.running)

        asyncio.create_task(self._run_subprocess(task_id, prompt))

    async def _run_subprocess(self, task_id: str, prompt: str) -> None:
        task = app_state.get_task(task_id)
        if not task:
            return

        agent = app_state.get_agent(task.agent_id)
        command = agent.command if agent else "cco"

        # Build command args
        args = [
            command,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
            prompt,
        ]

        # If resuming a previous session, add --continue with session id
        session_id = self._session_ids.get(task_id)
        if session_id:
            args = [
                command,
                "-p",
                "--output-format", "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--dangerously-skip-permissions",
                "--resume", session_id,
                prompt,
            ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._processes[task_id] = proc

            assert proc.stdout is not None
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                    await self._handle_chunk(task_id, chunk)
                except json.JSONDecodeError:
                    await self._append_text(task_id, line)

            returncode = await proc.wait()
            await self._finish_task(
                task_id,
                TaskStatus.success if returncode == 0 else TaskStatus.failed,
            )
        except FileNotFoundError:
            logger.warning("Command %r not found, using mock mode for task %s", command, task_id)
            await self._run_mock(task_id, prompt)
        except Exception as exc:
            logger.exception("Subprocess error for task %s: %s", task_id, exc)
            await self._finish_task(task_id, TaskStatus.failed)
        finally:
            self._processes.pop(task_id, None)
            self._current_msg.pop(task_id, None)

    async def _run_mock(self, task_id: str, prompt: str) -> None:
        """Fallback mock mode when cco is not available."""
        task = app_state.get_task(task_id)
        if not task:
            return

        from server.routes_ws import broadcast

        msg = Message(role="agent", content="", streaming=True)
        task.messages.append(msg)
        self._current_msg[task_id] = msg

        await broadcast(
            {"type": "message", "task_id": task_id, "message": msg.model_dump()}
        )

        mock_text = f"I received your prompt: **{prompt}**\n\nProcessing...\n\n"
        for ch in mock_text:
            msg.content += ch
            await broadcast(
                {
                    "type": "message_chunk",
                    "task_id": task_id,
                    "message_id": msg.id,
                    "delta": ch,
                }
            )
            await asyncio.sleep(0.02)

        await asyncio.sleep(0.5)
        done_text = "Done! Task completed successfully."
        for ch in done_text:
            msg.content += ch
            await broadcast(
                {
                    "type": "message_chunk",
                    "task_id": task_id,
                    "message_id": msg.id,
                    "delta": ch,
                }
            )
            await asyncio.sleep(0.02)

        msg.streaming = False
        await broadcast(
            {"type": "message_done", "task_id": task_id, "message_id": msg.id}
        )

        await self._finish_task(task_id, TaskStatus.success)

    async def _handle_chunk(self, task_id: str, chunk: dict[str, Any]) -> None:
        """Process a stream-json line from cco."""
        from server.routes_ws import broadcast

        task = app_state.get_task(task_id)
        if not task:
            return

        chunk_type = chunk.get("type", "")

        # ── system init: capture session_id ──
        if chunk_type == "system" and chunk.get("subtype") == "init":
            sid = chunk.get("session_id")
            if sid:
                self._session_ids[task_id] = sid
            return

        # ── stream_event: real-time deltas ──
        if chunk_type == "stream_event":
            event = chunk.get("event", {})
            event_type = event.get("type", "")

            if event_type == "message_start":
                # New assistant message – create streaming placeholder
                msg = Message(role="agent", content="", streaming=True)
                task.messages.append(msg)
                self._current_msg[task_id] = msg
                await broadcast(
                    {"type": "message", "task_id": task_id, "message": msg.model_dump()}
                )

            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                text = delta.get("text", "")
                msg = self._current_msg.get(task_id)
                if msg and text:
                    msg.content += text
                    await broadcast(
                        {
                            "type": "message_chunk",
                            "task_id": task_id,
                            "message_id": msg.id,
                            "delta": text,
                        }
                    )

            elif event_type == "content_block_stop":
                msg = self._current_msg.get(task_id)
                if msg:
                    msg.streaming = False
                    await broadcast(
                        {"type": "message_done", "task_id": task_id, "message_id": msg.id}
                    )
                    self._current_msg.pop(task_id, None)

            return

        # ── assistant: complete message (after streaming) ──
        if chunk_type == "assistant":
            # The full message arrives after all stream_event deltas.
            # If we already have a current_msg from stream_events, just
            # make sure its content matches the final text.
            message_data = chunk.get("message", {})
            content_blocks = message_data.get("content", [])
            full_text = ""
            for block in content_blocks:
                if block.get("type") == "text":
                    full_text += block.get("text", "")

            msg = self._current_msg.pop(task_id, None)
            if msg:
                # Update to the authoritative final content
                msg.content = full_text
                msg.streaming = False
            elif full_text:
                # No streaming was captured (fallback) – create message directly
                msg = Message(role="agent", content=full_text, streaming=False)
                task.messages.append(msg)
                await broadcast(
                    {"type": "message", "task_id": task_id, "message": msg.model_dump()}
                )
            return

        # ── result: process finished ──
        if chunk_type == "result":
            subtype = chunk.get("subtype", "")
            is_error = chunk.get("is_error", False)
            if is_error or subtype == "error":
                await self._finish_task(task_id, TaskStatus.failed)
            # success is handled by returncode in _run_subprocess
            return

    async def _append_text(self, task_id: str, text: str) -> None:
        """Append plain text to current message or create a new one."""
        from server.routes_ws import broadcast

        task = app_state.get_task(task_id)
        if not task:
            return

        msg = self._current_msg.get(task_id)
        if not msg:
            msg = Message(role="agent", content="", streaming=True)
            task.messages.append(msg)
            self._current_msg[task_id] = msg
            await broadcast(
                {"type": "message", "task_id": task_id, "message": msg.model_dump()}
            )

        msg.content += text + "\n"
        await broadcast(
            {
                "type": "message_chunk",
                "task_id": task_id,
                "message_id": msg.id,
                "delta": text + "\n",
            }
        )

    async def _finish_task(self, task_id: str, status: TaskStatus) -> None:
        task = app_state.get_task(task_id)
        if task and task.status not in (TaskStatus.success, TaskStatus.failed):
            task.status = status
        await self._broadcast_status(task_id, status)

    async def _broadcast_status(self, task_id: str, status: TaskStatus) -> None:
        from server.routes_ws import broadcast

        await broadcast(
            {"type": "task_status", "task_id": task_id, "status": status.value}
        )

    async def send_input(self, task_id: str, user_input: str) -> None:
        """Send user reply to agent. Since cco -p is non-interactive,
        we resume the session with a new subprocess."""
        task = app_state.get_task(task_id)
        if not task:
            return

        task.status = TaskStatus.running
        await self._broadcast_status(task_id, TaskStatus.running)

        # Kill current process if still running
        proc = self._processes.pop(task_id, None)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                proc.kill()

        # Start a new subprocess resuming the session
        asyncio.create_task(self._run_subprocess(task_id, user_input))

    async def kill_task(self, task_id: str) -> None:
        """Terminate subprocess for a task."""
        proc = self._processes.pop(task_id, None)
        if proc:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                proc.kill()
        self._session_ids.pop(task_id, None)


runner = AgentRunner()
