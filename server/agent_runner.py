"""Agent subprocess manager – spawns cco and manages I/O.

cco requires a TTY to produce output, so we use Python's pty module
to allocate a pseudo-terminal for the child process.

cco stream-json protocol (with --include-partial-messages):
  {"type":"system","subtype":"init", ...}
  {"type":"stream_event","event":{"type":"message_start", ...}}
  {"type":"stream_event","event":{"type":"content_block_start","content_block":{"type":"text"|"tool_use", ...}}}
  {"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta"|"input_json_delta", ...}}}
  {"type":"stream_event","event":{"type":"content_block_stop", ...}}
  {"type":"assistant","message":{"content":[{"type":"text","text":"..."}, {"type":"tool_use","name":"...","input":{...}}], ...}}
  {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"...","content":"..."}]}}
  {"type":"result","subtype":"success"|"error", ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pty
import signal
from pathlib import Path
from typing import Any

from server.models import Message, TaskStatus
from server.state import app_state

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"


class AgentRunner:
    def __init__(self) -> None:
        self._pids: dict[str, int] = {}           # task_id -> child pid
        self._master_fds: dict[str, int] = {}     # task_id -> pty master fd
        self._current_msg: dict[str, Message] = {}
        self._current_block_type: dict[str, str] = {}  # task_id -> "text"|"tool_use"
        self._session_ids: dict[str, str] = self._load_sessions()

    def _load_sessions(self) -> dict[str, str]:
        """Load session IDs from disk."""
        if SESSIONS_FILE.exists():
            try:
                return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("Failed to load session IDs")
        return {}

    def _save_sessions(self) -> None:
        """Persist session IDs to disk (atomic write)."""
        DATA_DIR.mkdir(exist_ok=True)
        tmp = SESSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._session_ids, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(SESSIONS_FILE)

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
        agent_cwd = agent.cwd if agent and agent.cwd else ""

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
                if agent_cwd:
                    os.chdir(agent_cwd)
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
            self._current_block_type.pop(task_id, None)

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
                self._save_sessions()
            return

        # ── stream_event: real-time deltas ──
        if chunk_type == "stream_event":
            event = chunk.get("event", {})
            event_type = event.get("type", "")

            if event_type == "message_start":
                msg = Message(role="agent", content="", streaming=True)
                task.messages.append(msg)
                self._current_msg[task_id] = msg
                self._current_block_type.pop(task_id, None)
                await broadcast(
                    {"type": "message", "task_id": task_id, "message": msg.model_dump()}
                )

            elif event_type == "content_block_start":
                content_block = event.get("content_block", {})
                block_type = content_block.get("type", "text")
                self._current_block_type[task_id] = block_type

                if block_type == "tool_use":
                    # Start a new tool_use message
                    tool_name = content_block.get("name", "")
                    # Close the current text message if streaming
                    cur = self._current_msg.get(task_id)
                    if cur and cur.streaming and cur.type == "text":
                        cur.streaming = False
                        await broadcast(
                            {"type": "message_done", "task_id": task_id, "message_id": cur.id}
                        )

                    msg = Message(
                        role="agent", type="tool_use", content="",
                        tool_name=tool_name, streaming=True,
                    )
                    task.messages.append(msg)
                    self._current_msg[task_id] = msg
                    await broadcast(
                        {"type": "message", "task_id": task_id, "message": msg.model_dump()}
                    )

                elif block_type == "text":
                    # If there's no current text message, create one
                    cur = self._current_msg.get(task_id)
                    if not cur or cur.type != "text" or not cur.streaming:
                        msg = Message(role="agent", content="", streaming=True)
                        task.messages.append(msg)
                        self._current_msg[task_id] = msg
                        await broadcast(
                            {"type": "message", "task_id": task_id, "message": msg.model_dump()}
                        )

            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                delta_type = delta.get("type", "")
                msg = self._current_msg.get(task_id)

                if delta_type == "text_delta":
                    text = delta.get("text", "")
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

                elif delta_type == "input_json_delta":
                    partial = delta.get("partial_json", "")
                    if msg and partial and msg.type == "tool_use":
                        msg.content += partial
                        await broadcast(
                            {
                                "type": "message_chunk",
                                "task_id": task_id,
                                "message_id": msg.id,
                                "delta": partial,
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
                self._current_block_type.pop(task_id, None)

            return

        # ── assistant: complete message (after streaming) ──
        # The streaming phase (stream_event) already created messages and
        # broadcast all deltas to the frontend.  The assistant chunk carries
        # the final, authoritative content – we use it ONLY to fix up the
        # server-side copies (for persistence accuracy) without broadcasting
        # anything to the frontend, which already has the correct content.
        if chunk_type == "assistant":
            message_data = chunk.get("message", {})
            content_blocks = message_data.get("content", [])

            # Close any still-streaming message
            cur = self._current_msg.pop(task_id, None)
            if cur and cur.streaming:
                cur.streaming = False
                await broadcast(
                    {"type": "message_done", "task_id": task_id, "message_id": cur.id}
                )

            # Collect existing messages by type for matching
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

            app_state.save_tasks()
            return

        # ── user: tool_result ──
        if chunk_type == "user":
            message_data = chunk.get("message", {})
            content_blocks = message_data.get("content", [])
            for block in content_blocks:
                if block.get("type") == "tool_result":
                    result_content = block.get("content", "")
                    # Try to get a readable summary from tool_use_result
                    tool_result_meta = chunk.get("tool_use_result", {})
                    if isinstance(result_content, list):
                        # Some tool results are lists of content blocks
                        parts = []
                        for part in result_content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                parts.append(part.get("text", ""))
                        result_content = "\n".join(parts)
                    if not isinstance(result_content, str):
                        result_content = json.dumps(result_content, ensure_ascii=False)

                    msg = Message(
                        role="agent", type="tool_result",
                        content=result_content, streaming=False,
                    )
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
        from server.routes_ws import broadcast

        # Close any still-streaming message before finishing the task
        msg = self._current_msg.pop(task_id, None)
        if msg and msg.streaming:
            msg.streaming = False
            await broadcast(
                {"type": "message_done", "task_id": task_id, "message_id": msg.id}
            )

        task = app_state.get_task(task_id)
        if task and task.status not in (TaskStatus.success, TaskStatus.failed):
            task.status = status
        await self._broadcast_status(task_id, status)
        app_state.save_tasks()

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
