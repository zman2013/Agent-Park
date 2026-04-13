"""Agent subprocess manager – spawns agent processes and manages I/O.

Supports multiple agent protocols via the adapter pattern:
  - cco/ccs: PTY-based, stream-json protocol (CcoAdapter)
  - codex: pipe-based, JSONL protocol (CodexAdapter)

Protocol-specific logic (command building, chunk handling) is delegated to
adapters in server/adapters/. This module handles subprocess lifecycle,
session management, and the ChunkContext callback interface.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import pty
import signal
import threading
from pathlib import Path
from typing import Any

from server.adapters import get_adapter
from server.adapters.base import BaseAdapter
from server.models import Message, TaskStatus
from server.models import _utcnow as _model_utcnow
from server.state import app_state

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"


class _RunContext:
    """Implements ChunkContext — callback interface for adapters.

    Each _run_subprocess call creates one _RunContext.  The adapter calls
    methods on this object to create messages, update tokens, save sessions,
    and finish the task.
    """

    def __init__(self, runner: AgentRunner, task_id: str) -> None:
        self._runner = runner
        self.task_id = task_id

    # ── session management ──────────────────────────────────────────────

    async def save_session(self, session_id: str) -> None:
        from server.routes_ws import broadcast

        prev_sid = self._runner._session_ids.get(self.task_id)
        if prev_sid and session_id != prev_sid:
            await self.on_session_renewed(prev_sid, session_id)
        else:
            self._runner._session_ids[self.task_id] = session_id
            self._runner._save_sessions()
        await broadcast({
            "type": "session_update",
            "task_id": self.task_id,
            "session_id": session_id,
        })

    async def on_session_renewed(self, old_sid: str, new_sid: str) -> None:
        logger.warning(
            "Session changed for task %s: %s -> %s (auto-renewing)",
            self.task_id, old_sid, new_sid,
        )
        self._runner._session_ids[self.task_id] = new_sid
        self._runner._save_sessions()
        self._runner._session_renewed.add(self.task_id)
        await self.send_system_notice(
            "会话已过期，已自动切换至新会话继续对话。（历史上下文已重置）"
        )

    # ── message creation ────────────────────────────────────────────────

    async def create_message(
        self,
        role: str,
        type_: str,
        content: str,
        tool_name: str = "",
        streaming: bool = False,
    ) -> Message:
        from server.routes_ws import broadcast

        task = app_state.get_task(self.task_id)
        if not task:
            # Should not happen, but return a dummy message
            return Message(role=role, type=type_, content=content, streaming=streaming)

        msg = Message(
            role=role, type=type_, content=content,
            tool_name=tool_name, streaming=streaming,
        )
        task.messages.append(msg)
        await broadcast({
            "type": "message",
            "task_id": self.task_id,
            "message": msg.model_dump(),
        })
        return msg

    async def append_delta(self, message_id: str, text: str) -> None:
        from server.routes_ws import broadcast

        await broadcast({
            "type": "message_chunk",
            "task_id": self.task_id,
            "message_id": message_id,
            "delta": text,
        })

    async def close_message(self, message_id: str) -> None:
        from server.routes_ws import broadcast

        await broadcast({
            "type": "message_done",
            "task_id": self.task_id,
            "message_id": message_id,
        })

    async def send_system_notice(self, content: str) -> None:
        from server.routes_ws import broadcast

        task = app_state.get_task(self.task_id)
        if not task:
            return
        notice = Message(
            role="agent", type="system", streaming=False, content=content,
        )
        task.messages.append(notice)
        await broadcast({
            "type": "message",
            "task_id": self.task_id,
            "message": notice.model_dump(),
        })

    # ── token accounting ────────────────────────────────────────────────

    async def update_tokens(
        self,
        input_tokens: int,
        output_tokens: int,
        model: str = "",
        context_window: int = 0,
    ) -> None:
        from server.routes_ws import broadcast

        task = app_state.get_task(self.task_id)
        if not task:
            return

        task.total_input_tokens += input_tokens
        task.total_output_tokens += output_tokens
        if context_window:
            task.context_window = context_window
        if model:
            if model not in task.model_usage:
                task.model_usage[model] = {
                    "inputTokens": 0, "outputTokens": 0,
                    "contextWindow": context_window,
                }
            task.model_usage[model]["inputTokens"] += input_tokens
            task.model_usage[model]["outputTokens"] += output_tokens
            if context_window:
                task.model_usage[model]["contextWindow"] = context_window

        await broadcast({
            "type": "turns_info",
            "task_id": self.task_id,
            "num_turns": task.num_turns,
            "total_input_tokens": task.total_input_tokens,
            "total_output_tokens": task.total_output_tokens,
            "context_window": task.context_window,
            "total_cost_cny": task.total_cost_cny,
            "model_usage": task.model_usage,
        })

    async def apply_authoritative_usage(
        self,
        model_usage: dict[str, dict],
    ) -> None:
        """Apply authoritative session-total token usage from a result chunk.

        The result chunk's modelUsage is the definitive total for THIS session.
        We merge it onto the baseline snapshot captured at session start.
        """
        task = app_state.get_task(self.task_id)
        if not task:
            return

        baseline = self._runner._session_baselines.get(self.task_id, {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost_cny": 0.0,
            "model_usage": {},
        })

        session_input = 0
        session_output = 0
        session_model_usage: dict = {}
        for _model, mu in model_usage.items():
            in_tok = (
                mu.get("inputTokens", 0)
                + mu.get("cacheReadInputTokens", 0)
                + mu.get("cacheCreationInputTokens", 0)
            )
            out_tok = mu.get("outputTokens", 0)
            ctx_win = mu.get("contextWindow", 0)
            session_input += in_tok
            session_output += out_tok
            if ctx_win:
                task.context_window = ctx_win
            session_model_usage[_model] = {
                "inputTokens": in_tok,
                "outputTokens": out_tok,
                "contextWindow": ctx_win,
            }

        # Merge session totals onto baseline
        task.total_input_tokens = baseline["total_input_tokens"] + session_input
        task.total_output_tokens = baseline["total_output_tokens"] + session_output
        merged_model_usage = dict(baseline["model_usage"])
        for _model, mu in session_model_usage.items():
            if _model in merged_model_usage:
                merged_model_usage[_model] = {
                    "inputTokens": merged_model_usage[_model]["inputTokens"] + mu["inputTokens"],
                    "outputTokens": merged_model_usage[_model]["outputTokens"] + mu["outputTokens"],
                    "contextWindow": mu["contextWindow"] or merged_model_usage[_model].get("contextWindow", 0),
                }
            else:
                merged_model_usage[_model] = mu
        task.model_usage = merged_model_usage

    # ── task finish (called by adapter for result-bearing protocols) ────

    async def finish(
        self,
        status: TaskStatus,
        num_turns: int = 0,
        cost_usd: float = 0,
        result_text: str = "",
        errors: list[str] | None = None,
    ) -> None:
        """Handle protocol-level task completion (e.g. cco result chunk).

        For protocols without an explicit result chunk (codex), the runner
        calls _finish_task directly based on process exit code.
        """
        from server.routes_ws import broadcast

        task = app_state.get_task(self.task_id)
        if not task:
            return

        # Accumulate turns
        task.num_turns += num_turns

        # Token/cost accounting from result chunk (cco-specific authoritative totals)
        baseline = self._runner._session_baselines.get(self.task_id, {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost_cny": 0.0,
            "model_usage": {},
        })

        if cost_usd:
            task.total_cost_cny = baseline["total_cost_cny"] + cost_usd * 7.3

        # Broadcast final turns_info
        await broadcast({
            "type": "turns_info",
            "task_id": self.task_id,
            "num_turns": task.num_turns,
            "total_input_tokens": task.total_input_tokens,
            "total_output_tokens": task.total_output_tokens,
            "context_window": task.context_window,
            "total_cost_cny": task.total_cost_cny,
            "model_usage": task.model_usage,
        })

        # Handle result text (may contain content not yet streamed)
        if result_text:
            result_text = result_text.strip()
            last_text_msg = None
            for m in reversed(task.messages):
                if m.role == "agent" and m.type == "text":
                    last_text_msg = m
                    break

            existing_content = (last_text_msg.content if last_text_msg else "").strip()

            if not existing_content or not result_text.startswith(existing_content):
                if result_text != existing_content:
                    msg = Message(role="agent", content=result_text, streaming=False)
                    task.messages.append(msg)
                    await broadcast({
                        "type": "message",
                        "task_id": self.task_id,
                        "message": msg.model_dump(),
                    })
            elif len(result_text) > len(existing_content):
                delta = result_text[len(existing_content):]
                if last_text_msg:
                    last_text_msg.content = result_text
                    last_text_msg.streaming = False
                    await broadcast({
                        "type": "message_chunk",
                        "task_id": self.task_id,
                        "message_id": last_text_msg.id,
                        "delta": delta,
                    })
                    await broadcast({
                        "type": "message_done",
                        "task_id": self.task_id,
                        "message_id": last_text_msg.id,
                    })

        # Max-turns detection — auto-resume with "继续"
        if status == TaskStatus.success and not result_text:
            last_text_msg = None
            for m in reversed(task.messages):
                if m.role == "agent" and m.type == "text":
                    last_text_msg = m
                    break
            if last_text_msg and (last_text_msg.streaming or len(last_text_msg.content) < 200):
                if last_text_msg.streaming:
                    last_text_msg.streaming = False
                    await broadcast({
                        "type": "message_done",
                        "task_id": self.task_id,
                        "message_id": last_text_msg.id,
                    })
                notice_text = f"已达到单次会话 turns 上限（本轮 {num_turns} turns），已自动回复'继续'让 agent 继续工作。"
                await self.send_system_notice(notice_text)
                # Auto-resume: restart subprocess with "继续"
                await self._runner.send_input(
                    self.task_id,
                    "继续",
                    kill_existing=False,
                )
                # Yield so the new task claims ownership before we clean up
                await asyncio.sleep(0)
                return

        # Silent success detection
        if status == TaskStatus.success and not result_text:
            has_agent_text = any(
                m.role == "agent" and m.type == "text" for m in task.messages
            )
            if not has_agent_text:
                await self.send_system_notice("任务已完成。")

        # Error handling
        if errors:
            # Check for session auto-renewal
            if self.task_id in self._runner._session_renewed:
                self._runner._session_renewed.discard(self.task_id)
                await self._runner._finish_task(self.task_id, TaskStatus.success)
                return

            joined = "；".join(errors)
            if "No conversation found" in joined:
                old_sid = self._runner._session_ids.pop(self.task_id, None)
                if old_sid:
                    self._runner._save_sessions()
                    logger.warning(
                        "Session expired for task %s (sid=%s), cleared from store",
                        self.task_id, old_sid,
                    )
                notice_content = f"会话已失效，下次发送消息将重新开始会话。（原因：{joined}）"
            else:
                notice_content = f"执行出错：{joined}"
            await self.send_system_notice(notice_content)

        await self._runner._finish_task(self.task_id, status)


class AgentRunner:
    def __init__(self) -> None:
        self._pids: dict[str, int] = {}           # task_id -> child pid
        self._master_fds: dict[str, int] = {}     # task_id -> pty master fd
        self._async_procs: dict[str, asyncio.subprocess.Process] = {}  # task_id -> pipe-mode proc
        self._adapters: dict[str, BaseAdapter] = {}  # task_id -> active adapter
        self._session_ids: dict[str, str] = self._load_sessions()
        self._resuming: set[str] = set()          # task_ids being killed for resume
        self._session_renewed: set[str] = set()   # task_ids whose session auto-renewed
        self._subprocess_tasks: dict[str, asyncio.Task] = {}  # task_id -> asyncio.Task
        # Snapshot of cumulative token/cost values at the START of each session.
        self._session_baselines: dict[str, dict] = {}  # task_id -> baseline snapshot
        # Track which run owns shared resources (_adapters, _session_baselines)
        # to prevent a finishing run from cleaning up a resumed run's state.
        self._run_ids: dict[str, str] = {}  # task_id -> unique run id

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

    def _start_subprocess(
        self,
        task_id: str,
        prompt: str,
        *,
        cancel_existing: bool = True,
    ) -> None:
        """Create and track an asyncio Task for _run_subprocess to prevent GC."""
        old = self._subprocess_tasks.get(task_id)
        current = asyncio.current_task()
        if (
            cancel_existing
            and old
            and not old.done()
            and old is not current
        ):
            old.cancel()

        t = asyncio.create_task(
            self._run_subprocess(task_id, prompt),
            name=f"subprocess-{task_id}",
        )
        self._subprocess_tasks[task_id] = t

        def _on_done(fut: asyncio.Task) -> None:
            if self._subprocess_tasks.get(task_id) is fut:
                self._subprocess_tasks.pop(task_id, None)

        t.add_done_callback(_on_done)

    async def _run_subprocess(self, task_id: str, prompt: str) -> None:
        import uuid as _uuid
        run_id = _uuid.uuid4().hex[:8]

        task = app_state.get_task(task_id)
        if not task:
            return

        # Snapshot cumulative totals before this session starts
        self._session_baselines[task_id] = {
            "total_input_tokens": task.total_input_tokens,
            "total_output_tokens": task.total_output_tokens,
            "total_cost_cny": task.total_cost_cny,
            "model_usage": copy.deepcopy(task.model_usage),
        }

        # Claim ownership for this run
        self._run_ids[task_id] = run_id

        # Ensure running status is set
        if task.status not in (TaskStatus.running,):
            task.status = TaskStatus.running
            await self._broadcast_status(task_id, TaskStatus.running)

        agent = app_state.get_agent(task.agent_id)
        command = agent.command if agent else "cco"
        agent_cwd = agent.cwd if agent and agent.cwd else ""

        session_id = self._session_ids.get(task_id)

        # Check if this is a fork: task has a fork_session_id to consume
        fork_sid = task.fork_session_id
        if fork_sid:
            task.fork_session_id = None  # consume once
            app_state.save_agent_tasks(task.agent_id)

        # Inject memory only on new sessions (no existing session_id and not a fork)
        if not session_id and not fork_sid:
            from server.memory import load_memory
            from server.config import memory_config
            mem_cfg = memory_config()
            memory_lines = load_memory(task.agent_id, mem_cfg["max_lines"])
            if memory_lines:
                memory_text = "\n".join(memory_lines)
                prompt = f"<memory>\n{memory_text}\n</memory>\n\n{prompt}"

        # Select adapter and build args
        adapter = get_adapter(command)
        self._adapters[task_id] = adapter
        args = adapter.build_args(command, prompt, session_id, fork_sid, agent_cwd)
        ctx = _RunContext(self, task_id)

        # Validate working directory before spawning subprocess
        if agent_cwd and not os.path.isdir(agent_cwd):
            from server.routes_ws import broadcast
            err_msg = f"工作目录不存在：{agent_cwd}\n请检查 Agent 配置中的路径是否正确。"
            notice = Message(role="agent", type="system", streaming=False, content=err_msg)
            task.messages.append(notice)
            await broadcast({"type": "message", "task_id": task_id, "message": notice.model_dump()})
            await self._finish_task(task_id, TaskStatus.failed)
            self._cleanup_run_resources(task_id, run_id)
            return

        try:
            if adapter.needs_pty():
                await self._run_pty_mode(task_id, args, agent_cwd, adapter, ctx, run_id)
            else:
                await self._run_pipe_mode(task_id, args, agent_cwd, adapter, ctx, run_id)

        except FileNotFoundError:
            logger.warning("Command %r not found, using mock mode for task %s", command, task_id)
            await self._run_mock(task_id, prompt)
        except Exception as exc:
            logger.exception("Subprocess error for task %s: %s", task_id, exc)
            await self._finish_task(task_id, TaskStatus.failed)
        finally:
            # Only clean up if this run still owns the resources (not replaced by resume)
            self._cleanup_run_resources(task_id, run_id)
            # Fallback: if the task is still marked running AND this run still owns it, mark failed
            if self._run_ids.get(task_id) in (run_id, None):
                task = app_state.get_task(task_id)
                if task and task.status == TaskStatus.running:
                    await self._finish_task(task_id, TaskStatus.failed)

    def _cleanup_run_resources(self, task_id: str, run_id: str) -> None:
        """Clean up shared resources only if this run still owns them."""
        if self._run_ids.get(task_id) != run_id:
            return
        # Another run has claimed ownership — don't steal its resources
        self._run_ids.pop(task_id, None)
        self._pids.pop(task_id, None)
        self._master_fds.pop(task_id, None)
        self._async_procs.pop(task_id, None)
        self._adapters.pop(task_id, None)
        self._session_baselines.pop(task_id, None)
        # Clear persisted PID — subprocess is gone
        task = app_state.get_task(task_id)
        if task:
            object.__setattr__(task, "subprocess_pid", None)
            app_state.save_agent_tasks(task.agent_id)

    # ── PTY mode (cco/ccs) ──────────────────────────────────────────────

    async def _run_pty_mode(
        self,
        task_id: str,
        args: list[str],
        agent_cwd: str,
        adapter: BaseAdapter,
        ctx: _RunContext,
        run_id: str = "",
    ) -> None:
        master_fd, slave_fd = pty.openpty()

        pid = os.fork()
        if pid == 0:
            # ── Child process ──
            os.close(master_fd)
            os.setsid()
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            if agent_cwd:
                os.chdir(agent_cwd)
            env = _clean_env()
            os.execvpe(args[0], args, env)
            # execvpe does not return
        else:
            # ── Parent process ──
            os.close(slave_fd)
            self._pids[task_id] = pid
            self._master_fds[task_id] = master_fd
            # Persist PID to task for orphan recovery
            task = app_state.get_task(task_id)
            if task:
                object.__setattr__(task, "subprocess_pid", pid)
                app_state.save_agent_tasks(task.agent_id)

            logger.info("Spawned %s pid=%d for task %s (pty mode)", args[0], pid, task_id)

            loop = asyncio.get_event_loop()

            wait_future: asyncio.Future = loop.create_future()

            def _wait_child():
                try:
                    _, st = os.waitpid(pid, 0)
                    rc = os.WEXITSTATUS(st) if os.WIFEXITED(st) else -1
                except ChildProcessError:
                    rc = -1
                if not wait_future.done():
                    loop.call_soon_threadsafe(wait_future.set_result, rc)

            threading.Thread(target=_wait_child, daemon=True).start()

            reader = asyncio.StreamReader()
            read_protocol = asyncio.StreamReaderProtocol(reader)
            read_transport, _ = await loop.connect_read_pipe(
                lambda: read_protocol, os.fdopen(master_fd, "rb", 0)
            )

            def _on_ept_exit(fut: asyncio.Future) -> None:
                read_transport.close()
                rc = fut.result() if not fut.cancelled() else -1
                status = TaskStatus.success if rc == 0 else TaskStatus.failed
                # Skip if this run no longer owns the task (replaced by auto-resume)
                if self._run_ids.get(task_id) != run_id:
                    return
                agent_task = app_state.get_task(task_id)
                if agent_task and agent_task.status == TaskStatus.running:
                    asyncio.ensure_future(self._finish_task(task_id, status))

            wait_future.add_done_callback(_on_ept_exit)

            await self._read_json_lines(reader, read_transport, adapter, ctx, strip_ansi=True)

            returncode = await wait_future
            logger.info("%s pid=%d exited with code %d", args[0], pid, returncode)

            # Skip if this run no longer owns the task (replaced by auto-resume)
            if self._run_ids.get(task_id) == run_id:
                await self._finish_task(
                    task_id,
                    TaskStatus.success if returncode == 0 else TaskStatus.failed,
                )

    # ── Pipe mode (codex) ───────────────────────────────────────────────

    async def _run_pipe_mode(
        self,
        task_id: str,
        args: list[str],
        agent_cwd: str,
        adapter: BaseAdapter,
        ctx: _RunContext,
        run_id: str = "",
    ) -> None:
        env = _clean_env()

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=agent_cwd or None,
            env=env,
        )
        self._async_procs[task_id] = proc

        logger.info("Spawned %s pid=%d for task %s (pipe mode)", args[0], proc.pid, task_id)

        # Read stdout line by line
        assert proc.stdout is not None
        buf = b""
        try:
            while True:
                try:
                    chunk = await proc.stdout.read(65536)
                except Exception:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                        await adapter.handle_chunk(parsed, ctx)
                    except json.JSONDecodeError:
                        pass
        finally:
            pass

        returncode = await proc.wait()
        logger.info("%s pid=%d exited with code %d", args[0], proc.pid, returncode)

        # Skip if this run no longer owns the task (replaced by auto-resume)
        if self._run_ids.get(task_id) == run_id:
            await self._finish_task(
                task_id,
                TaskStatus.success if returncode == 0 else TaskStatus.failed,
            )

    # ── shared JSON line reader ─────────────────────────────────────────

    async def _read_json_lines(
        self,
        reader: asyncio.StreamReader,
        transport: asyncio.ReadTransport,
        adapter: BaseAdapter,
        ctx: _RunContext,
        strip_ansi: bool = False,
    ) -> None:
        """Read and dispatch JSON lines from a stream."""
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
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if strip_ansi:
                        line = _strip_ansi(line)
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                        await adapter.handle_chunk(parsed, ctx)
                    except json.JSONDecodeError:
                        pass
        finally:
            transport.close()

    # ── mock mode ───────────────────────────────────────────────────────

    async def _run_mock(self, task_id: str, prompt: str) -> None:
        """Fallback mock mode when command is not available."""
        task = app_state.get_task(task_id)
        if not task:
            return

        from server.routes_ws import broadcast

        msg = Message(role="agent", content="", streaming=True)
        task.messages.append(msg)

        await broadcast(
            {"type": "message", "task_id": task_id, "message": msg.model_dump()}
        )

        mock_text = f"I received your prompt: **{prompt}**\n\nProcessing...\n\n"
        for ch in mock_text:
            msg.content += ch
            await broadcast({
                "type": "message_chunk",
                "task_id": task_id,
                "message_id": msg.id,
                "delta": ch,
            })
            await asyncio.sleep(0.02)

        await asyncio.sleep(0.5)
        done_text = "Done! Task completed successfully."
        for ch in done_text:
            msg.content += ch
            await broadcast({
                "type": "message_chunk",
                "task_id": task_id,
                "message_id": msg.id,
                "delta": ch,
            })
            await asyncio.sleep(0.02)

        msg.streaming = False
        await broadcast(
            {"type": "message_done", "task_id": task_id, "message_id": msg.id}
        )

        await self._finish_task(task_id, TaskStatus.success)

    # ── task finish ─────────────────────────────────────────────────────

    async def _finish_task(self, task_id: str, status: TaskStatus) -> None:
        from server.routes_ws import broadcast

        # If this task is being killed for a resume, don't overwrite the running status
        if status == TaskStatus.failed and task_id in self._resuming:
            self._resuming.discard(task_id)
            return

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

    # ── user input (resume) ─────────────────────────────────────────────

    async def send_input(
        self,
        task_id: str,
        user_input: str,
        *,
        kill_existing: bool = True,
    ) -> None:
        """Send user reply to agent via --resume."""
        task = app_state.get_task(task_id)
        if not task:
            return

        task.status = TaskStatus.running
        task.updated_at = _model_utcnow()
        await self._broadcast_status(task_id, TaskStatus.running)

        if kill_existing:
            # Mark as resuming so the dying subprocess doesn't overwrite status with failed
            self._resuming.add(task_id)
            # Kill current process if still running
            await self.kill_task(task_id)

        # Start a new subprocess resuming the session
        self._start_subprocess(task_id, user_input, cancel_existing=kill_existing)

    # ── kill ────────────────────────────────────────────────────────────

    async def kill_task(self, task_id: str) -> None:
        """Terminate subprocess for a task."""
        # PTY mode: kill by pid
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

        # Pipe mode: kill asyncio subprocess
        proc = self._async_procs.pop(task_id, None)
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            await asyncio.sleep(0.5)
            try:
                proc.kill()
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

    # ── orphan task restore ─────────────────────────────────────────────

    def restore_orphan_tasks(self) -> list[str]:
        """Terminate orphan tasks whose subprocess outlived the server.

        After a server restart, the PTY master fd is lost — we cannot recover
        the I/O channel to read agent output or detect actual task state.
        The only safe action is to kill the surviving process and mark the
        task as failed so the user can retry.

        Called once during startup (after lifespan).  Returns the list of
        task_ids that were cleaned up so the caller can broadcast status.
        """
        cleaned: list[str] = []
        for task in list(app_state.tasks.values()):
            if task.status.value not in ("running", "waiting"):
                continue
            pid = getattr(task, "subprocess_pid", None)
            task_id = task.id
            if pid is None:
                # No PID persisted — the task was started in a previous
                # server version or never had one.  Mark as failed.
                task.status = TaskStatus.failed
                logger.info(
                    "Orphan task %s has no PID, marking as failed", task_id
                )
                app_state.save_agent_tasks(task.agent_id)
                cleaned.append(task_id)
                continue

            # Kill the surviving process — we've lost the PTY fd and
            # cannot recover the I/O channel.
            try:
                os.killpg(pid, signal.SIGTERM)
                logger.info("Sent SIGTERM to orphan pid %d (task %s)", pid, task_id)
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except Exception:
                    pass

            # Also reap zombie children
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            except Exception:
                pass

            task.status = TaskStatus.failed
            object.__setattr__(task, "subprocess_pid", None)
            logger.info(
                "Orphan task %s (pid=%d) terminated and marked as failed",
                task_id, pid,
            )
            app_state.save_agent_tasks(task.agent_id)
            cleaned.append(task_id)
        return cleaned


    async def shutdown(self) -> None:
        """Graceful shutdown: kill any tracked subprocesses."""
        for task_id, pid in list(self._pids.items()):
            try:
                os.killpg(pid, signal.SIGTERM)
            except Exception:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass


# ── helpers ─────────────────────────────────────────────────────────────

def _clean_env() -> dict[str, str]:
    """Return a copy of os.environ with virtualenv vars stripped."""
    env = os.environ.copy()
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("VIRTUAL_ENV_PROMPT", None)
    if venv:
        venv_bin = os.path.join(venv, "bin")
        path_parts = env.get("PATH", "").split(os.pathsep)
        path_parts = [p for p in path_parts if p != venv_bin]
        env["PATH"] = os.pathsep.join(path_parts)
    return env


def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from a string."""
    import re
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b\[[^\x40-\x7e]*[\x40-\x7e]", "", s)


runner = AgentRunner()
