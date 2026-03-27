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
import threading
from pathlib import Path
from typing import Any

from server.models import Message, TaskStatus
from server.models import _utcnow as _model_utcnow
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
        self._resuming: set[str] = set()          # task_ids being killed for resume
        self._session_renewed: set[str] = set()   # task_ids whose session auto-renewed
        self._subprocess_tasks: dict[str, asyncio.Task] = {}  # task_id -> asyncio.Task

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
        task.updated_at = _model_utcnow()
        await self._broadcast_status(task_id, TaskStatus.running)

        self._start_subprocess(task_id, prompt)

    def _start_subprocess(self, task_id: str, prompt: str) -> None:
        """Create and track an asyncio Task for _run_subprocess to prevent GC."""
        old = self._subprocess_tasks.pop(task_id, None)
        if old and not old.done():
            old.cancel()

        t = asyncio.create_task(
            self._run_subprocess(task_id, prompt),
            name=f"subprocess-{task_id}",
        )
        self._subprocess_tasks[task_id] = t

        def _on_done(fut: asyncio.Task) -> None:
            self._subprocess_tasks.pop(task_id, None)

        t.add_done_callback(_on_done)

    async def _run_subprocess(self, task_id: str, prompt: str) -> None:
        task = app_state.get_task(task_id)
        if not task:
            return

        # Ensure running status is set (send_input sets it before killing the old
        # process, but the old process's _finish_task could race and overwrite it
        # before this coroutine is scheduled; re-assert here to be safe)
        if task.status not in (TaskStatus.running,):
            task.status = TaskStatus.running
            await self._broadcast_status(task_id, TaskStatus.running)

        agent = app_state.get_agent(task.agent_id)
        command = agent.command if agent else "cco"
        agent_cwd = agent.cwd if agent and agent.cwd else ""

        session_id = self._session_ids.get(task_id)

        # Inject memory only on new sessions (no existing session_id)
        if not session_id:
            from server.memory import load_memory
            from server.config import memory_config
            mem_cfg = memory_config()
            memory_lines = load_memory(task.agent_id, mem_cfg["max_lines"])
            if memory_lines:
                memory_text = "\n".join(memory_lines)
                prompt = f"<memory>\n{memory_text}\n</memory>\n\n{prompt}"

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

        # Validate working directory before spawning subprocess
        if agent_cwd and not os.path.isdir(agent_cwd):
            from server.routes_ws import broadcast
            err_msg = f"工作目录不存在：{agent_cwd}\n请检查 Agent 配置中的路径是否正确。"
            notice = Message(role="agent", type="system", streaming=False, content=err_msg)
            task.messages.append(notice)
            await broadcast({"type": "message", "task_id": task_id, "message": notice.model_dump()})
            await self._finish_task(task_id, TaskStatus.failed)
            return

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
                # Strip virtualenv env vars so they don't leak into the agent process
                env = os.environ.copy()
                venv = env.pop("VIRTUAL_ENV", None)
                env.pop("VIRTUAL_ENV_PROMPT", None)
                if venv:
                    venv_bin = os.path.join(venv, "bin")
                    path_parts = env.get("PATH", "").split(os.pathsep)
                    path_parts = [p for p in path_parts if p != venv_bin]
                    env["PATH"] = os.pathsep.join(path_parts)
                os.execvpe(args[0], args, env)
                # execvp does not return
            else:
                # ── Parent process ──
                os.close(slave_fd)
                self._pids[task_id] = pid
                self._master_fds[task_id] = master_fd

                logger.info("Spawned cco pid=%d for task %s", pid, task_id)

                loop = asyncio.get_event_loop()

                # Wait for the direct child (ept wrapper) in a background thread.
                # When ept exits, close master_fd so the pty read loop gets EOF,
                # even if the grandchild (claude) is still alive.
                wait_future: asyncio.Future = loop.create_future()

                def _wait_child():
                    try:
                        _, st = os.waitpid(pid, 0)
                        rc = os.WEXITSTATUS(st) if os.WIFEXITED(st) else -1
                    except ChildProcessError:
                        rc = -1
                    # Guard against the asyncio Task being destroyed before this
                    # callback fires (e.g. task was killed for resume).
                    if not wait_future.done():
                        loop.call_soon_threadsafe(wait_future.set_result, rc)

                threading.Thread(target=_wait_child, daemon=True).start()

                # Read from master_fd asynchronously
                reader = asyncio.StreamReader()
                read_protocol = asyncio.StreamReaderProtocol(reader)
                read_transport, _ = await loop.connect_read_pipe(
                    lambda: read_protocol, os.fdopen(master_fd, "rb", 0)
                )

                # When ept exits: close transport (unblocks reader.read) AND
                # ensure _finish_task is called even if the asyncio Task is destroyed.
                def _on_ept_exit(fut: asyncio.Future) -> None:
                    # Always close transport so read loop unblocks
                    read_transport.close()
                    # If the coroutine Task was GC'd, finish the agent task here
                    rc = fut.result() if not fut.cancelled() else -1
                    status = TaskStatus.success if rc == 0 else TaskStatus.failed
                    agent_task = app_state.get_task(task_id)
                    if agent_task and agent_task.status == TaskStatus.running:
                        asyncio.ensure_future(self._finish_task(task_id, status))

                wait_future.add_done_callback(_on_ept_exit)

                closer_task = None  # transport is now closed via callback

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

                returncode = await wait_future
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
            # Fallback: if the task is still marked running (e.g. asyncio Task was
            # destroyed before _finish_task could be called), mark it failed so the
            # UI stops showing a permanent yellow spinner.
            task = app_state.get_task(task_id)
            if task and task.status == TaskStatus.running:
                await self._finish_task(task_id, TaskStatus.failed)

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
                prev_sid = self._session_ids.get(task_id)
                if prev_sid and sid != prev_sid:
                    # A new session was opened instead of resuming — the previous
                    # session likely expired.  Save the new session ID so that
                    # subsequent turns use the renewed session, and mark this task
                    # so the result chunk's is_error=true is treated as a soft
                    # renewal rather than a hard failure.
                    logger.warning(
                        "Session changed for task %s: %s -> %s (auto-renewing)",
                        task_id, prev_sid, sid,
                    )
                    self._session_ids[task_id] = sid
                    self._save_sessions()
                    self._session_renewed.add(task_id)
                    # Notify the user that a new session has started
                    t = app_state.get_task(task_id)
                    if t:
                        notice = Message(
                            role="agent", type="system", streaming=False,
                            content="会话已过期，已自动切换至新会话继续对话。（历史上下文已重置）",
                        )
                        t.messages.append(notice)
                        await broadcast(
                            {"type": "message", "task_id": task_id, "message": notice.model_dump()}
                        )
                else:
                    self._session_ids[task_id] = sid
                    self._save_sessions()
                await broadcast({"type": "session_update", "task_id": task_id, "session_id": sid})
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
            logger.info("assistant chunk for task %s: %d content blocks", task_id, len(chunk.get("message", {}).get("content", [])))
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

            app_state.save_agent_tasks(task.agent_id)
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
            logger.info("result chunk for task %s: %s", task_id, json.dumps(chunk, ensure_ascii=False)[:2000])
            subtype = chunk.get("subtype", "")
            is_error = chunk.get("is_error", False)
            num_turns = chunk.get("num_turns", 0)

            # Update cumulative turn count on the task
            task.num_turns += num_turns

            # Extract token usage from modelUsage (preferred) or usage fallback
            model_usage = chunk.get("modelUsage", {})
            if model_usage:
                for _model, mu in model_usage.items():
                    in_tok = mu.get("inputTokens", 0) + mu.get("cacheReadInputTokens", 0) + mu.get("cacheCreationInputTokens", 0)
                    out_tok = mu.get("outputTokens", 0)
                    ctx_win = mu.get("contextWindow", 0)
                    task.total_input_tokens += in_tok
                    task.total_output_tokens += out_tok
                    if ctx_win:
                        task.context_window = ctx_win
                    # Accumulate per-model usage
                    if _model not in task.model_usage:
                        task.model_usage[_model] = {"inputTokens": 0, "outputTokens": 0, "contextWindow": ctx_win}
                    task.model_usage[_model]["inputTokens"] += in_tok
                    task.model_usage[_model]["outputTokens"] += out_tok
                    if ctx_win:
                        task.model_usage[_model]["contextWindow"] = ctx_win
            else:
                usage = chunk.get("usage", {})
                in_tok = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                task.total_input_tokens += in_tok
                task.total_output_tokens += out_tok

            cost_usd = chunk.get("total_cost_usd", 0) or 0
            task.total_cost_cny += cost_usd * 7.3

            await broadcast({
                "type": "turns_info",
                "task_id": task_id,
                "num_turns": task.num_turns,
                "total_input_tokens": task.total_input_tokens,
                "total_output_tokens": task.total_output_tokens,
                "context_window": task.context_window,
                "total_cost_cny": task.total_cost_cny,
                "model_usage": task.model_usage,
            })

            # The result chunk may carry a "result" text field that contains
            # content not yet streamed (e.g. when AskUserQuestion is denied).
            # Compare with the last agent text message and send the delta if needed.
            result_text = chunk.get("result", "")
            if result_text and isinstance(result_text, str):
                result_text = result_text.strip()
                # Find the last agent text message
                last_text_msg = None
                for m in reversed(task.messages):
                    if m.role == "agent" and m.type == "text":
                        last_text_msg = m
                        break

                existing_content = (last_text_msg.content if last_text_msg else "").strip()

                if not existing_content or not result_text.startswith(existing_content):
                    # result text is different from streamed content — send it as a new message
                    if result_text != existing_content:
                        msg = Message(role="agent", content=result_text, streaming=False)
                        task.messages.append(msg)
                        await broadcast(
                            {"type": "message", "task_id": task_id, "message": msg.model_dump()}
                        )
                elif len(result_text) > len(existing_content):
                    # result text extends the streamed content — append the delta
                    delta = result_text[len(existing_content):]
                    if last_text_msg:
                        last_text_msg.content = result_text
                        last_text_msg.streaming = False
                        await broadcast(
                            {
                                "type": "message_chunk",
                                "task_id": task_id,
                                "message_id": last_text_msg.id,
                                "delta": delta,
                            }
                        )
                        await broadcast(
                            {"type": "message_done", "task_id": task_id, "message_id": last_text_msg.id}
                        )

            # Detect max-turns exit: success but result text is empty and the last
            # streamed message is truncated (still streaming or very short).
            if subtype == "success" and not result_text:
                last_text_msg = None
                for m in reversed(task.messages):
                    if m.role == "agent" and m.type == "text":
                        last_text_msg = m
                        break
                if last_text_msg and (last_text_msg.streaming or len(last_text_msg.content) < 200):
                    # Close the truncated message first
                    if last_text_msg.streaming:
                        last_text_msg.streaming = False
                        await broadcast(
                            {"type": "message_done", "task_id": task_id, "message_id": last_text_msg.id}
                        )
                    notice = Message(
                        role="agent", type="system", streaming=False,
                        content=f"已达到单次会话 turns 上限（本轮 {num_turns} turns），输出被截断。发送任意消息可继续。",
                    )
                    task.messages.append(notice)
                    await broadcast(
                        {"type": "message", "task_id": task_id, "message": notice.model_dump()}
                    )

            # If task succeeded silently (no result text and no agent text messages
            # were streamed), send a default completion notice so the user gets feedback.
            if subtype == "success" and not result_text:
                has_agent_text = any(
                    m.role == "agent" and m.type == "text"
                    for m in task.messages
                )
                if not has_agent_text:
                    notice = Message(
                        role="agent", type="system", streaming=False,
                        content="任务已完成。",
                    )
                    task.messages.append(notice)
                    await broadcast(
                        {"type": "message", "task_id": task_id, "message": notice.model_dump()}
                    )

            if is_error or subtype in ("error", "error_during_execution"):
                # If this error was caused by a session auto-renewal, treat it as
                # success so the task doesn't get marked failed.
                if task_id in self._session_renewed:
                    self._session_renewed.discard(task_id)
                    await self._finish_task(task_id, TaskStatus.success)
                    return
                error_msgs = chunk.get("errors", [])
                if error_msgs:
                    joined = "；".join(str(e) for e in error_msgs)
                    # Detect session expiry and clear stale session ID
                    if "No conversation found" in joined:
                        old_sid = self._session_ids.pop(task_id, None)
                        if old_sid:
                            self._save_sessions()
                            logger.warning(
                                "Session expired for task %s (sid=%s), cleared from store",
                                task_id, old_sid,
                            )
                        notice_content = f"会话已失效，下次发送消息将重新开始会话。（原因：{joined}）"
                    else:
                        notice_content = f"执行出错：{joined}"
                    notice = Message(
                        role="agent", type="system", streaming=False,
                        content=notice_content,
                    )
                    task.messages.append(notice)
                    await broadcast(
                        {"type": "message", "task_id": task_id, "message": notice.model_dump()}
                    )
                await self._finish_task(task_id, TaskStatus.failed)
            elif subtype == "success":
                await self._finish_task(task_id, TaskStatus.success)
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

        # If this task is being killed for a resume, don't overwrite the running status
        if status == TaskStatus.failed and task_id in self._resuming:
            self._resuming.discard(task_id)
            return

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
            task.updated_at = _model_utcnow()
        await self._broadcast_status(task_id, task.status if task else status)
        if task:
            app_state.save_agent_tasks(task.agent_id)

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
        task.updated_at = _model_utcnow()
        await self._broadcast_status(task_id, TaskStatus.running)

        # Mark as resuming so the dying subprocess doesn't overwrite status with failed
        self._resuming.add(task_id)
        # Kill current process if still running
        await self.kill_task(task_id)

        # Start a new subprocess resuming the session
        self._start_subprocess(task_id, user_input)

    async def kill_task(self, task_id: str) -> None:
        """Terminate subprocess for a task."""
        pid = self._pids.pop(task_id, None)
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            # Give it a moment to exit
            await asyncio.sleep(0.5)
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        fd = self._master_fds.pop(task_id, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        t = self._subprocess_tasks.pop(task_id, None)
        if t and not t.done():
            t.cancel()


def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from a string."""
    import re
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b\[[^\x40-\x7e]*[\x40-\x7e]", "", s)


runner = AgentRunner()
