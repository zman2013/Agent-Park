"""Agent subprocess manager – spawns cco and manages I/O.

cco requires a TTY to produce output, so we use Python's pty module
to allocate a pseudo-terminal for the child process.

cco stream-json protocol (with --include-partial-messages):
  {"type":"system","subtype":"init", ...}
  {"type":"stream_event","event":{"type":"message_start", ...}}
  {"type":"stream_event","event":{"type":"content_block_start", ...}}
  {"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}}
  {"type":"stream_event","event":{"type":"content_block_stop", ...}}
  {"type":"assistant","message":{"content":[{"type":"text","text":"..."}], ...}}
  {"type":"result","subtype":"success"|"error", ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pty
import signal
from typing import Any

from server.models import Message, TaskStatus
from server.state import app_state

logger = logging.getLogger(__name__)


class AgentRunner:
    def __init__(self) -> None:
        self._pids: dict[str, int] = {}           # task_id -> child pid
        self._master_fds: dict[str, int] = {}     # task_id -> pty master fd
        self._current_msg: dict[str, Message] = {}
        self._session_ids: dict[str, str] = {}     # task_id -> cco session_id

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
            # Use pty to give cco a pseudo-terminal (it requires TTY)
            master_fd, slave_fd = pty.openpty()

            pid = os.fork()
            if pid == 0:
                # ── Child process ──
                os.close(master_fd)
                os.setsid()
                # Set slave as stdin/stdout/stderr
                os.dup2(slave_fd, 0)
                os.dup2(slave_fd, 1)
                os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                os.execvp(args[0], args)
                # execvp does not return
            else:
                # ── Parent process ──
                os.close(slave_fd)
                self._pids[task_id] = pid
                self._master_fds[task_id] = master_fd

                logger.info("Spawned cco pid=%d for task %s", pid, task_id)

                # Read from master_fd asynchronously
                loop = asyncio.get_event_loop()
                reader = asyncio.StreamReader()
                read_protocol = asyncio.StreamReaderProtocol(reader)
                read_transport, _ = await loop.connect_read_pipe(
                    lambda: read_protocol, os.fdopen(master_fd, "rb", 0)
                )

                buf = b""
                try:
                    while True:
                        try:
                            chunk = await reader.read(65536)
                        except Exception:
                            break
                        if not chunk:
                            break
                        buf += chunk
                        # Process complete lines
                        while b"\n" in buf:
                            raw_line, buf = buf.split(b"\n", 1)
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            # Strip ANSI escape sequences
                            line = _strip_ansi(line)
                            if not line:
                                continue
                            try:
                                parsed = json.loads(line)
                                await self._handle_chunk(task_id, parsed)
                            except json.JSONDecodeError:
                                # Skip non-JSON lines (terminal control codes etc)
                                pass
                finally:
                    read_transport.close()

                # Wait for child to finish
                _, status = await loop.run_in_executor(None, os.waitpid, pid, 0)
                returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1
                logger.info("cco pid=%d exited with code %d", pid, returncode)

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
            self._pids.pop(task_id, None)
            self._master_fds.pop(task_id, None)
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
            message_data = chunk.get("message", {})
            content_blocks = message_data.get("content", [])
            full_text = ""
            for block in content_blocks:
                if block.get("type") == "text":
                    full_text += block.get("text", "")

            msg = self._current_msg.pop(task_id, None)
            if msg:
                msg.content = full_text
                msg.streaming = False
            elif full_text:
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
        """Send user reply to agent via --resume."""
        task = app_state.get_task(task_id)
        if not task:
            return

        task.status = TaskStatus.running
        await self._broadcast_status(task_id, TaskStatus.running)

        # Kill current process if still running
        await self.kill_task(task_id)

        # Start a new subprocess resuming the session
        asyncio.create_task(self._run_subprocess(task_id, user_input))

    async def kill_task(self, task_id: str) -> None:
        """Terminate subprocess for a task."""
        pid = self._pids.pop(task_id, None)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                # Give it a moment to exit
                await asyncio.sleep(0.5)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
        fd = self._master_fds.pop(task_id, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from a string."""
    import re
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b\[[^\x40-\x7e]*[\x40-\x7e]", "", s)


runner = AgentRunner()
