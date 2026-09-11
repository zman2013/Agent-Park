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
import time
from pathlib import Path
from typing import Any

from server import process_group as pg
from server.adapters import get_adapter
from server.adapters.base import BaseAdapter
from server.models import Message, Task, TaskStatus
from server.models import _utcnow as _model_utcnow
from server.state import app_state

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"

# shutdown()'s drain window for pending Feishu notifications. BASE exceeds the
# CLI's own 30s timeout with margin (so its kill-the-child handler gets to
# run). Same-task notifications coalesce instead of queueing, so the worst case
# is a fixed BASE × task_notify.MAX_SERIAL_SENDS — a constant, not a sampled
# depth (which could miss a coroutine scheduled but not yet started). Must stay
# under run.sh's force-kill grace for the backend.
NOTIFY_DRAIN_BASE_SECONDS = 40

# 通知被取消后，留给它 kill-and-reap CLI 子进程的时间。send_feishu_card 的取消分支
# 走 SIGKILL 再 reap，正常瞬间完成；这个窗口是为"取消一定有机会跑完"而存在的下限。
#
# 为什么必须单独留额度：17s（SIGTERM/SIGKILL/cancel）+ 80s（通知 drain）+ 13s
# （consolidation drain/cancel）= 110s，本来就装不进 90s 总预算，通知 drain 注定被
# 压缩。被压缩本身可以接受（最坏是丢掉最新那张卡，下次启动没有补发义务），但绝不能连
# 收尾都没时间跑 —— 那样 feishu-bot CLI 子进程会在后端退出后成为孤儿。
NOTIFY_KILL_SECONDS = 5

# shutdown()'s drain window for history-triggered consolidations. Deliberately
# small: the notify drain above can already consume 80s of run.sh's 95s
# force-kill grace, so there is no room to wait out a consolidation's LLM calls
# (600s timeout each) and it is not necessary to. An interrupted consolidation
# loses nothing — write_layer is atomic via os.replace, and the history counter
# is only reset after both layers clear the gates, so the window is retried
# instead of dropped. This wait exists only to let a consolidation that is
# already past its LLM calls finish writing.
CONSOLIDATE_DRAIN_SECONDS = 5

# After cancelling, how long to let the kill-and-reap path in _llm_call run.
# Its own reap wait is 5s, so this must exceed it or shutdown returns while the
# child is still being collected.
CONSOLIDATE_KILL_SECONDS = 8


# shutdown() 的总预算。run.sh 的 backend stop grace 是 95s（见 do_stop）：超时它就
# kill -9 后端，届时通知卡的收尾、consolidation 的 kill-and-reap 都不会再跑（前者丢卡，
# 后者把 glm/cco 子进程留成孤儿）。各阶段若各自独立计时再相加，最坏情况是
# 10(SIGTERM) + 5(SIGKILL) + 2(cancel) + 80(notify) + 5 + 8(consolidate) = 110s，
# 已经越过那道线。所以所有等待共享这一个截止时间，而不是逐段累加。
# 留 5s 余量给 uvicorn 自身的 lifespan/连接收尾。
SHUTDOWN_TOTAL_BUDGET_SECONDS = 90

# 进程组两阶段各自的上限（仍与总预算取小）。抽成常量+访问器，和下面通知/consolidation
# 的窗口同一套写法：数字有名字可讲，测试也能压小它们而不必真的等满 10+5 秒。
SHUTDOWN_SIGTERM_GRACE_SECONDS = 10
SHUTDOWN_SIGKILL_GRACE_SECONDS = 5


def _sigterm_grace_seconds() -> int:
    return SHUTDOWN_SIGTERM_GRACE_SECONDS


def _sigkill_grace_seconds() -> int:
    return SHUTDOWN_SIGKILL_GRACE_SECONDS


def _shutdown_total_budget_seconds() -> int:
    return SHUTDOWN_TOTAL_BUDGET_SECONDS


def _consolidate_drain_seconds() -> int:
    return CONSOLIDATE_DRAIN_SECONDS


def _consolidate_kill_seconds() -> int:
    return CONSOLIDATE_KILL_SECONDS


def _notify_drain_max_seconds() -> int:
    from server.task_notify import MAX_SERIAL_SENDS

    return NOTIFY_DRAIN_BASE_SECONDS * MAX_SERIAL_SENDS


def _notify_kill_seconds() -> int:
    return NOTIFY_KILL_SECONDS


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

    async def close_message(self, message_id: str, content: str | None = None) -> None:
        from server.routes_ws import broadcast

        payload = {
            "type": "message_done",
            "task_id": self.task_id,
            "message_id": message_id,
        }
        # Replacement, for adapters whose bubble text is only final on close.
        # A delta cannot express it when the text is revised rather than
        # extended — a codex sub-agent's status goes running -> completed.
        if content is not None:
            payload["content"] = content
        await broadcast(payload)

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

        await self._maybe_warn_compact_pending(task, model, input_tokens, context_window)

    async def attach_usage(
        self,
        message_id: str,
        usage: dict,
    ) -> None:
        """Attach per-turn LLM usage to a specific message and broadcast it.

        Adapters call this on the LAST agent message of each turn so the
        frontend can render a per-message usage badge. Silently no-ops when
        the message is no longer present (e.g. task deleted mid-stream).
        """
        from server.routes_ws import broadcast

        task = app_state.get_task(self.task_id)
        if not task:
            return
        target = None
        for m in task.messages:
            if m.id == message_id:
                target = m
                break
        if target is None:
            return
        target.usage = usage
        payload: dict = {
            "type": "message_usage",
            "task_id": self.task_id,
            "message_id": message_id,
            "usage": usage,
        }
        if getattr(target, "cco_uuid", None):
            payload["cco_uuid"] = target.cco_uuid
        await broadcast(payload)

    async def _maybe_warn_compact_pending(
        self,
        task,
        model: str,
        current_input_tokens: int,
        current_context_window: int,
    ) -> None:
        """Emit a one-shot '即将压缩' notice when THIS turn's input tokens cross the threshold.

        Uses absolute token counts (config.compact.warn_tokens /
        auto_compact_tokens) rather than ratios against a context window.
        Ratios were unreliable because cco's assistant chunk usually omits
        ``context_window`` and model key names diverge between the assistant
        chunk and the authoritative result chunk, so the ratio code had to
        guess the window and mis-fired on 1M models.

        Must use the current turn's input tokens, not the cumulative model total —
        the cumulative sum grows unboundedly with turn count and would falsely trigger
        even when the latest request is far below the compact threshold.

        Also marks ``_compact_pending`` when the auto-compact threshold is
        crossed, so the runner can dispatch ``/compact`` after the current
        turn finishes.
        """
        from server.config import compact_config
        cfg = compact_config()
        warn_tokens = cfg["warn_tokens"]
        auto_tokens = cfg["auto_compact_tokens"]
        auto_enabled = cfg["auto_compact_enabled"]

        if not model:
            return
        used = current_input_tokens
        if not used:
            return

        # Auto-compact: latch a pending flag when used tokens cross the auto threshold.
        # The actual /compact dispatch happens after finish() returns control,
        # so we don't interrupt the in-flight turn.
        task_disabled = self.task_id in self._runner._auto_compact_disabled
        if auto_enabled and not task_disabled and auto_tokens and used >= auto_tokens:
            self._runner._compact_pending.add(self.task_id)

        # Warning notification (one-shot per compact cycle). Skip when disabled.
        if task_disabled:
            return
        if self.task_id in self._runner._compact_warned:
            return
        if not warn_tokens or used < warn_tokens:
            return
        self._runner._compact_warned.add(self.task_id)
        notice = (
            f"⚠️ 上下文接近压缩阈值（{model} 本轮使用 {used:,} tokens，"
            f"阈值 {warn_tokens:,}），下一轮可能触发自动压缩（compact），"
            "期间可能有较长时间无输出。"
        )
        await self.send_system_notice(notice)

    async def reset_compact_warning(self) -> None:
        """Called by adapters after a compact_boundary so the next round can warn again."""
        self._runner._compact_warned.discard(self.task_id)
        self._runner._compact_pending.discard(self.task_id)

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

        # Backfill per-message usage.context_window for any earlier turn whose
        # assistant chunk omitted it (cco often only emits the authoritative
        # window in this final result chunk). Without this, the per-message
        # badge would persist `context_window: 0` and lose the `/window (%)`
        # display for the very turns this feature is meant to cover.
        from server.routes_ws import broadcast
        for msg in task.messages:
            u = getattr(msg, "usage", None)
            if not u or u.get("context_window"):
                continue
            model_name = u.get("model") or ""
            backfilled_win = (
                merged_model_usage.get(model_name, {}).get("contextWindow", 0)
                or task.context_window
            )
            if not backfilled_win:
                continue
            u["context_window"] = backfilled_win
            await broadcast({
                "type": "message_usage",
                "task_id": self.task_id,
                "message_id": msg.id,
                "usage": u,
            })

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

        # Auto-compact: if this turn crossed the auto-compact threshold and the
        # turn finished cleanly, dispatch `/compact` as the next user input.
        # The cco harness intercepts /compact and runs the compact procedure
        # itself, after which a `compact_boundary` chunk clears the pending
        # flag. We skip on errors (errors path already returned upstream when
        # session-renewed) so a failed turn doesn't immediately retry-as-compact.
        # On the skipped path we still clear the pending flag so the next
        # successful turn doesn't inherit a stale /compact dispatch (e.g. after
        # a session-expired error clears the session and the user starts a
        # fresh low-context turn).
        await self._runner.maybe_dispatch_auto_compact(
            self.task_id,
            success=(status == TaskStatus.success and not errors),
        )
        await self._runner.maybe_dispatch_handoff(
            self.task_id,
            success=(status == TaskStatus.success and not errors),
        )


class AgentRunner:
    def __init__(self) -> None:
        self._pids: dict[str, int] = {}           # task_id -> child pid
        self._master_fds: dict[str, int] = {}     # task_id -> pty master fd
        self._async_procs: dict[str, asyncio.subprocess.Process] = {}  # task_id -> pipe-mode proc
        # task_id -> leader's /proc start time, read at spawn. Gates every signal
        # so a recycled pid/pgid cannot absorb a kill aimed at our group.
        self._proc_starts: dict[str, int | None] = {}
        # task_ids whose kill_task ended without proof that the group is gone
        # (闸门因 pg.GROUP_UNKNOWN 拒发，或组扛过了 SIGKILL)。_cleanup_run_resources
        # 见到它就保留 subprocess_pid，让下次启动的 restore_orphan_tasks 再试一次。
        # 只管"这一轮别把 pid 清掉"；跨 resume 的把手另记在任务的 retained_pgids
        # 元数据里（见 _record_retained_pgid），因为新进程会覆盖 subprocess_pid。
        self._retain_pid: set[str] = set()
        self._adapters: dict[str, BaseAdapter] = {}  # task_id -> active adapter
        self._session_ids: dict[str, str] = self._load_sessions()
        self._resuming: set[str] = set()          # task_ids being killed for resume
        self._session_renewed: set[str] = set()   # task_ids whose session auto-renewed
        self._compact_warned: set[str] = set()    # task_ids that already got a compact warning this round
        self._compact_pending: set[str] = set()   # task_ids slated to receive an auto /compact after this turn
        self._auto_compact_disabled: set[str] = set()  # task_ids with auto-compact manually disabled
        self._plan_mode: set[str] = set()          # task_ids running in plan (read-only) mode
        # task_ids whose handoff turn just finished — broadcast completion notice next.
        self._handoff_pending: set[str] = set()
        self._subprocess_tasks: dict[str, asyncio.Task] = {}  # task_id -> asyncio.Task
        self._notify_tasks: set[asyncio.Task] = set()  # detached feishu-notify tasks (survive runner cancellation)
        self._consolidate_tasks: set[asyncio.Task] = set()  # detached history-triggered consolidations
        self._consolidating: set[str] = set()  # eids with a consolidation in flight
        # PTY read transports currently awaiting EOF, so shutdown() can force
        # them closed instead of waiting out their internal 60s lingering-
        # grandchild safety net (see _run_pty_mode) within its own bounded
        # stop window.
        self._pty_read_transports: dict[str, asyncio.ReadTransport] = {}
        # Shutdown-only registry keyed by run_id (unique per _run_subprocess
        # call), NOT task_id. _pids/_async_procs/_pty_read_transports/
        # _subprocess_tasks are all keyed by task_id and represent "the
        # current run for this task" — a kill_existing=False continuation
        # (max-turns auto-resume, auto-compact) can register its own run
        # under the same task_id while the previous run is still finishing
        # (e.g. draining a lingering PTY writer), overwriting those entries
        # and making the old run invisible to shutdown(). This registry never
        # gets overwritten by an unrelated run, so shutdown() can terminate
        # and await every live run, not just the newest one per task_id.
        self._live_runs: dict[str, dict] = {}  # run_id -> {task, pid, proc, transport}
        # Index into task.messages marking the start of the CURRENT run, so
        # notification can scan only this run's output — internal continuations
        # (/compact, handoff auto-resume) don't append a user Message, so a
        # role=="user" boundary can't detect them.
        self._run_start_index: dict[str, int] = {}  # task_id -> message count when this run started
        # Snapshot of cumulative token/cost values at the START of each session.
        self._session_baselines: dict[str, dict] = {}  # task_id -> baseline snapshot
        # Track which run owns shared resources (_adapters, _session_baselines)
        # to prevent a finishing run from cleaning up a resumed run's state.
        self._run_ids: dict[str, str] = {}  # task_id -> unique run id
        # Serializes "is the task busy? then take it" across every input path
        # (WS user_message, feishu inbound). Both append a Message and await
        # before send_input() flips status to running, so without a shared lock
        # two concurrent inputs each start a run and the second's kill_existing
        # tears down the first's subprocess, dropping its input.
        self._input_locks: dict[str, asyncio.Lock] = {}
        # Persistent-memory block per task, built once on first run and reused
        # for every resume so the system-prompt cache prefix stays byte-stable
        # within one task's lifetime. Deliberately not persisted: 1782 tasks x
        # ~4 KB would mean rewriting a 7 MB JSON on every new task, and the only
        # consequence of losing it is that a task resumed after a restart picks
        # up newer memory.
        self._memory_snapshots: dict[str, str] = {}

    def input_lock(self, task_id: str) -> asyncio.Lock:
        """Return the per-task lock guarding check-status-then-send_input."""
        lock = self._input_locks.get(task_id)
        if lock is None:
            lock = self._input_locks[task_id] = asyncio.Lock()
        return lock

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

        import uuid as _uuid
        run_id = _uuid.uuid4().hex[:8]
        # Registered under run_id (not task_id) so shutdown() can always find
        # and terminate every live run — a kill_existing=False continuation
        # (max-turns auto-resume, auto-compact) can register its own run
        # under the same task_id while the previous run is still finishing
        # (e.g. draining a lingering PTY writer), overwriting the task_id-
        # keyed _subprocess_tasks/_pids/_async_procs/_pty_read_transports
        # entries below and making the old run invisible to a task_id-keyed
        # lookup.
        self._live_runs[run_id] = {"task_id": task_id}

        t = asyncio.create_task(
            self._run_subprocess(task_id, prompt, run_id),
            name=f"subprocess-{task_id}",
        )
        self._live_runs[run_id]["task"] = t
        self._subprocess_tasks[task_id] = t

        def _on_done(fut: asyncio.Task) -> None:
            if self._subprocess_tasks.get(task_id) is fut:
                self._subprocess_tasks.pop(task_id, None)
            # Guaranteed cleanup regardless of how the coroutine ended
            # (normal return, exception, or cancellation before it ever
            # reached its own try/finally — e.g. cancelled while awaiting
            # wiki search, or the task was deleted before this Task got its
            # first chance to run). Relying on _run_subprocess's internal
            # cleanup alone leaves _live_runs entries stale in exactly the
            # cases shutdown() most needs them gone: a cancelled/dead run
            # that will never call _finish_task on its own.
            self._live_runs.pop(run_id, None)

        t.add_done_callback(_on_done)

    async def _run_subprocess(self, task_id: str, prompt: str, run_id: str) -> None:
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
        self._run_start_index[task_id] = len(task.messages)

        try:
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
            fork_resume_at = task.fork_resume_at
            if fork_sid:
                task.fork_session_id = None  # consume once
                task.fork_resume_at = None
                app_state.save_agent_tasks(task.agent_id)

            # Strip !wiki prefix before any prompt augmentation
            skip_wiki = False
            if prompt.startswith("!wiki"):
                skip_wiki = True
                prompt = prompt[len("!wiki"):].lstrip()

            # Select adapter first: whether memory can ride in the system prompt
            # decides how it gets injected below.
            adapter = get_adapter(command)
            self._adapters[task_id] = adapter

            # Persistent memory. One snapshot per task, reused for every run:
            # the system prompt is a cacheable prefix, so identical bytes across
            # resumes keep the cache warm while a changed value would break it
            # mid-task.
            memory_context = self._memory_snapshots.get(task_id)
            if memory_context is None:
                from server import auto_memory
                memory_context = auto_memory.build_context(task.agent_id)
                self._memory_snapshots[task_id] = memory_context

            system_prompt = ""
            if adapter.supports_system_prompt():
                # Injected on every run, new session or resume alike, so memory
                # survives /compact and session renewal.
                system_prompt = memory_context

            # Inject wiki context only on new sessions (no existing session_id
            # and not a fork). It is a per-prompt search result, not persistent
            # memory, so it does not belong in the cached system prompt.
            if not session_id and not fork_sid:
                parts: list[str] = []
                if not system_prompt and memory_context:
                    # Adapter has no system-prompt channel: fall back to the
                    # prompt prefix, as before.
                    parts.append(memory_context)

                if not skip_wiki and agent and agent.wiki:
                    try:
                        from server.wiki_search import search_wiki
                        from server.routes_ws import broadcast
                        # Notify user that wiki search is starting
                        _notice_start = Message(
                            role="agent", type="system", streaming=False,
                            content=f"🔍 正在检索 wiki 知识库（{agent.wiki}）...",
                        )
                        task.messages.append(_notice_start)
                        await broadcast({"type": "message", "task_id": task_id, "message": _notice_start.model_dump()})

                        wiki_context = await search_wiki(prompt, agent.wiki)

                        if wiki_context:
                            parts.append(wiki_context)
                            _notice_done = Message(
                                role="agent", type="system", streaming=False,
                                content=f"📚 Wiki 检索完成，已注入上下文：\n\n{wiki_context}",
                            )
                        else:
                            _notice_done = Message(
                                role="agent", type="system", streaming=False,
                                content=f"📭 Wiki 检索完成，未找到相关页面。",
                            )
                        task.messages.append(_notice_done)
                        await broadcast({"type": "message", "task_id": task_id, "message": _notice_done.model_dump()})
                    except Exception:
                        logger.exception("Wiki search failed for task %s, skipping", task_id)

                parts.append(prompt)
                prompt = "\n\n".join(parts)

            args = adapter.build_args(
                command, prompt, session_id, fork_sid, agent_cwd,
                resume_at=fork_resume_at,
                plan_mode=task_id in self._plan_mode,
                system_prompt=system_prompt,
            )
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
        except asyncio.CancelledError:
            # Cancelled before any child was ever spawned (e.g. shutdown()
            # cancelling a run still stuck in search_wiki(), or a resume's
            # cancel_existing=True racing this same pre-spawn window). The
            # post-spawn try/finally below already finalizes on cancellation
            # via its own `finally`; this section has no such wrapper, so
            # without this the task would stay stuck at running with no
            # completion card. _finish_task no-ops via the _resuming flag
            # when this is a normal resume cancellation, same as the
            # post-spawn path.
            await self._finish_task(task_id, TaskStatus.failed)
            self._cleanup_run_resources(task_id, run_id)
            raise

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
        self._proc_starts.pop(task_id, None)
        self._adapters.pop(task_id, None)
        self._session_baselines.pop(task_id, None)
        # Note: _run_start_index is intentionally NOT cleared here, for the same
        # reason as _compact_warned below: stop_task (routes_ws.py) cancels the
        # subprocess Task via kill_task() *before* calling _finish_task directly,
        # so this cleanup and that finalization race. Popping the boundary here
        # would make the notification fall back to index 0 (the whole transcript)
        # depending on which runs first. The next _run_subprocess overwrites this
        # entry anyway, so leaving it stale between runs is harmless; forget_task
        # clears it on real teardown.
        # Note: _compact_warned is intentionally NOT cleared here. Per-run cleanup
        # fires at the end of every subprocess run (including max-turns resume and
        # short turns between the warn threshold and the compact boundary). The
        # flag must persist across those until either compact_boundary fires
        # (reset_compact_warning) or the task is fully killed/deleted (kill_task).
        # Clear persisted PID — subprocess is gone
        task = app_state.get_task(task_id)
        if task:
            # 除非 kill_task 没能证明组已消失：那时 pid 元数据是残留后代唯一的把手，
            # 清掉就等于把可恢复的孤儿换成永久的孤儿。留着最多下次启动多查一次。
            if task_id in self._retain_pid:
                self._retain_pid.discard(task_id)
            else:
                object.__setattr__(task, "subprocess_pid", None)
                object.__setattr__(task, "subprocess_start_time", None)
                app_state.save_agent_tasks(task.agent_id)

    async def maybe_dispatch_auto_compact(self, task_id: str, *, success: bool) -> None:
        """Dispatch `/compact` as next input if this turn crossed the auto-compact threshold.

        Called from every turn-completion path so the behavior works regardless
        of adapter (cco's result-chunk path via _RunContext.finish, or
        codex-style pipe/pty modes that finish on subprocess exit).

        Always clears `_compact_pending` so a failed turn does not leave a
        stale flag that would trigger /compact on the next unrelated success.

        Takes the shared per-task input lock and re-checks the status inside
        it: this runs from the turn-completion path, where the task is briefly
        terminal, so an input arriving in that window (a feishu reply, a
        browser message) can claim the task first. Dispatching anyway would
        start /compact with kill_existing=False alongside that run, leaving two
        live subprocesses fighting over the task-keyed registries.
        """
        if task_id not in self._compact_pending:
            return
        self._compact_pending.discard(task_id)
        if not success:
            return
        from server.routes_ws import broadcast
        async with self.input_lock(task_id):
            task = app_state.get_task(task_id)
            if task and task.status == TaskStatus.running:
                logger.info(
                    "Skipping auto /compact for task %s: another input already claimed it",
                    task_id,
                )
                return
            if task:
                notice = Message(
                    role="agent",
                    type="system",
                    streaming=False,
                    content="🤖 上下文已超过自动压缩阈值，正在自动发送 /compact 指令…",
                )
                task.messages.append(notice)
                await broadcast({
                    "type": "message",
                    "task_id": task_id,
                    "message": notice.model_dump(),
                })
            await self.send_input(task_id, "/compact", kill_existing=False)
        # Yield so the new run claims ownership before any pending
        # finally-block cleanup in the just-finished subprocess fires.
        await asyncio.sleep(0)

    def toggle_auto_compact(self, task_id: str, disabled: bool) -> None:
        """Enable or disable automatic /compact dispatch for a specific task."""
        if disabled:
            self._auto_compact_disabled.add(task_id)
            # Clear any pending flag that was already set this turn
            self._compact_pending.discard(task_id)
        else:
            self._auto_compact_disabled.discard(task_id)
        # Persist toggle state so it survives server restarts
        task = app_state.tasks.get(task_id)
        if task is not None:
            object.__setattr__(task, "auto_compact_disabled", disabled)
            app_state.save_agent_tasks(task.agent_id)

    def toggle_plan_mode(self, task_id: str, enabled: bool) -> None:
        """Enable or disable plan (read-only research) mode for a task.

        Takes effect on the NEXT run: build_args reads _plan_mode when the
        subprocess is spawned, so toggling mid-run does not affect the
        in-flight turn.
        """
        if enabled:
            self._plan_mode.add(task_id)
        else:
            self._plan_mode.discard(task_id)
        # Persist toggle state so it survives server restarts
        task = app_state.tasks.get(task_id)
        if task is not None:
            object.__setattr__(task, "plan_mode", enabled)
            app_state.save_agent_tasks(task.agent_id)

    async def maybe_dispatch_handoff(self, task_id: str, *, success: bool) -> None:
        """Dispatch a pending handoff as next input once this turn completes.

        Always clears ``_handoff_pending`` so a failed turn does not leak a
        stale flag onto an unrelated subsequent run.
        """
        if task_id not in self._handoff_pending:
            return
        self._handoff_pending.discard(task_id)

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
            env = _clean_env(task_id)
            os.execvpe(args[0], args, env)
            # execvpe does not return
        else:
            # ── Parent process ──
            os.close(slave_fd)
            self._pids[task_id] = pid
            self._master_fds[task_id] = master_fd
            # 与 pipe 路径一致：身份在 spawn 时读一次并记进 _live_runs，shutdown 的每
            # 一发信号都按它过闸门。等到 shutdown 时才现读的话，leader 可能已经被回收，
            # 读到的是复用者的身份 —— 那会把陌生人的组当成我们的登记进信号集合。
            leader_start = pg.read_proc_start_time(pid)
            if run_id in self._live_runs:
                self._live_runs[run_id]["pid"] = pid
                self._live_runs[run_id]["pid_start"] = leader_start
            # Persist PID to task for orphan recovery
            task = app_state.get_task(task_id)
            if task:
                object.__setattr__(task, "subprocess_pid", pid)
                object.__setattr__(task, "subprocess_start_time", leader_start)
                app_state.save_agent_tasks(task.agent_id)
            # 同 pipe 路径：元数据已换成这个新进程，旧 run 的保留标记不再适用。
            self._retain_pid.discard(task_id)

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

            if run_id in self._live_runs:
                # PTY 没有 pipe 的 proc 句柄，"这个 pid 还是不是我们自己的子进程"只能
                # 靠这个 future：未 done 即代表还没 waitpid 回收，号码不会被内核出让。
                # shutdown 用它决定能不能补读 leader 身份、能不能按自家子进程直投。
                self._live_runs[run_id]["reaped"] = wait_future

            reader = asyncio.StreamReader()
            read_protocol = asyncio.StreamReaderProtocol(reader)
            read_transport, _ = await loop.connect_read_pipe(
                lambda: read_protocol, os.fdopen(master_fd, "rb", 0)
            )
            self._pty_read_transports[task_id] = read_transport
            if run_id in self._live_runs:
                self._live_runs[run_id]["transport"] = read_transport

            # Safety net for a lingering grandchild that still holds the pty
            # slave fd open after the immediate child exits — in that case
            # read() on the master fd never sees EOF and _read_json_lines
            # would hang forever. This timer is intentionally generous (well
            # beyond how long draining a large buffered turn can legitimately
            # take under backpressure) and is cancelled below the moment the
            # read loop returns on its own, so it never races a slow-but-still
            # -progressing drain into truncating output. shutdown() closes
            # this transport directly (bounded by its own stop window) rather
            # than waiting out this timer, so a lingering grandchild at process
            # exit doesn't strand this coroutine past the 60s mark.
            lingering_writer_timer = None

            def _on_ept_exit(fut: asyncio.Future) -> None:
                nonlocal lingering_writer_timer
                lingering_writer_timer = loop.call_later(60.0, read_transport.close)

            wait_future.add_done_callback(_on_ept_exit)

            try:
                await self._read_json_lines(reader, read_transport, adapter, ctx, strip_ansi=True)
            finally:
                if lingering_writer_timer:
                    lingering_writer_timer.cancel()
                # Only remove if this run still owns the entry: kill_existing=False
                # continuations (max-turns auto-resume, auto-compact) start a new
                # _run_pty_mode for the same task_id without cancelling this one,
                # so if the new run has already registered its own transport by
                # the time this (stale, finishing) run reaches here, popping
                # unconditionally would strip shutdown()'s only handle on the
                # transport that's actually still in use.
                if self._pty_read_transports.get(task_id) is read_transport:
                    self._pty_read_transports.pop(task_id, None)

            returncode = await wait_future
            logger.info("%s pid=%d exited with code %d", args[0], pid, returncode)

            # Skip if this run no longer owns the task (replaced by auto-resume)
            if self._run_ids.get(task_id) == run_id:
                status = TaskStatus.success if returncode == 0 else TaskStatus.failed
                await self._finish_task(task_id, status)
                # Adapters that finish via _RunContext.finish (cco) handle
                # auto-compact dispatch themselves; this branch covers PTY
                # adapters that don't, and is a no-op when the flag isn't set.
                await self.maybe_dispatch_auto_compact(
                    task_id, success=(status == TaskStatus.success),
                )
                await self.maybe_dispatch_handoff(
                    task_id, success=(status == TaskStatus.success),
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
        env = _clean_env(task_id)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=agent_cwd or None,
            env=env,
            # Own process group, so kill_task/shutdown can signal the whole
            # tree via killpg like the PTY path already does (which gets its
            # group from os.setsid in the child). Agent commands are wrapper
            # scripts — `codexgpt` execs `ept codex`, which spawns a node
            # launcher, which spawns the real binary — and proc.terminate()
            # only reaches the outermost one. A surviving grandchild keeps
            # codex's per-thread writer lock held, so the next resume dies
            # with "thread ... already has an active writer" and the task can
            # never be continued.
            start_new_session=True,
        )
        self._async_procs[task_id] = proc
        # Read once, at spawn, while the pid provably still refers to this child.
        # Every later signal to this pgid is gated on it (see pg.killpg_verified) so
        # a reused number cannot inherit a kill aimed at our group.
        leader_start = pg.read_proc_start_time(proc.pid)
        self._proc_starts[task_id] = leader_start
        if run_id in self._live_runs:
            self._live_runs[run_id]["proc"] = proc
            # killpg target for shutdown(), which otherwise only has `proc`
            # and would again leave the grandchildren running.
            self._live_runs[run_id]["pid"] = proc.pid
            self._live_runs[run_id]["pid_start"] = leader_start

        # Persist PID for orphan recovery, same as the PTY path. Without this a
        # pipe-mode subprocess that outlives a server restart is invisible to
        # restore_orphan_tasks, and its lingering process group keeps holding
        # the thread writer lock.
        task = app_state.get_task(task_id)
        if task:
            object.__setattr__(task, "subprocess_pid", proc.pid)
            object.__setattr__(task, "subprocess_start_time", leader_start)
            app_state.save_agent_tasks(task.agent_id)
        # 元数据现在描述的是这个新进程，上一轮 kill_task 留下的保留标记对它无效
        # （resume 时旧 run 的 cleanup 可能因 run_id 不匹配提前返回而没消费掉它）。
        self._retain_pid.discard(task_id)

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
            status = TaskStatus.success if returncode == 0 else TaskStatus.failed
            await self._finish_task(task_id, status)
            # Pipe-mode adapters (codex) never call _RunContext.finish, so
            # dispatch /compact here when the auto-compact threshold fired
            # mid-turn via update_tokens.
            await self.maybe_dispatch_auto_compact(
                task_id, success=(status == TaskStatus.success),
            )
            await self.maybe_dispatch_handoff(
                task_id, success=(status == TaskStatus.success),
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
        was_terminal = task and task.status in (TaskStatus.success, TaskStatus.failed)
        if task and not was_terminal:
            task.status = status
            task.updated_at = _model_utcnow()

        # A pending auto-compact continuation means this success is not the
        # real end of the turn — maybe_dispatch_auto_compact (called right
        # after _finish_task returns) will flip the task back to running and
        # dispatch /compact. Notifying now would report "done" mid-turn and
        # again when the continuation actually finishes.
        compact_will_continue = (
            task is not None and status == TaskStatus.success and task_id in self._compact_pending
        )
        # Snapshot BEFORE the first await below: a concurrent send_input() for
        # the same task_id can flip task.status back to running and advance
        # _run_start_index to the next run while we're suspended, so anything
        # read after the await could belong to the wrong run.
        notify_args = None
        if task and not was_terminal and not compact_will_continue:
            notify_args = self._prepare_notify(task_id, task)

        # Same three boundaries as the notification, for the same reasons: skip
        # resume kills (handled above), skip idempotent re-entry (was_terminal),
        # and skip a success that auto-compact is about to continue. Read before
        # the await so the text belongs to this run.
        history_args = None
        if task and not was_terminal and not compact_will_continue:
            history_args = self._prepare_history(task_id, task, status)

        # Close any bubble the subprocess left open. A run that dies between
        # item.started and item.completed (crash, EOF, nonzero exit) otherwise
        # leaves streaming=True forever: this path never closed messages, and
        # only the explicit WS stop path swept them. Before save_agent_tasks
        # below, so the persisted transcript is clean too.
        if task and not was_terminal:
            await self._close_open_bubbles(task_id)

        await self._broadcast_status(task_id, task.status if task else status)
        if task:
            app_state.save_agent_tasks(task.agent_id)
        if notify_args:
            self._schedule_notify(*notify_args)
        if history_args:
            self._record_history(*history_args)

    async def _close_open_bubbles(self, task_id: str) -> None:
        """Mark every streaming message of *task_id* done and tell clients.

        Collected and marked synchronously before the first broadcast: a
        user_message is allowed while a task is running, so send_input() can
        append a new streaming message to this same list while we are suspended
        on a broadcast, and iterating the live list would then close a bubble
        belonging to the replacement run whose subprocess is still producing
        output.
        """
        from server.routes_ws import broadcast

        task = app_state.get_task(task_id)
        if not task:
            return
        stale = []
        for msg in task.messages:
            if msg.streaming:
                msg.streaming = False
                stale.append(msg.id)
        for message_id in stale:
            await broadcast({
                "type": "message_done",
                "task_id": task_id,
                "message_id": message_id,
            })

    def _prepare_history(self, task_id: str, task: Task,
                         status: TaskStatus) -> tuple[str, str, str, str] | None:
        """Build (eid, who, status, text) for the history append, or None.

        Called before the first await in _finish_task, so ``_run_start_index``
        still points at this run: a concurrent send_input() would advance it and
        we would summarize the wrong run.
        """
        from server import auto_memory
        from server.task_notify import _last_agent_text

        text = _last_agent_text(task, self._run_start_index.get(task_id, 0))
        if not text:
            return None
        agent = app_state.get_agent(task.agent_id)
        who = f"{agent.name if agent else task.agent_id} / {task.name}"
        return auto_memory.effective_id(task.agent_id), who, str(status.value), text

    def _record_history(self, eid: str, who: str, status: str, text: str) -> None:
        """Append to history.md and, every Nth append, consolidate.

        The append itself is synchronous and LLM-free — one file rewrite of at
        most 4KB. Consolidation is detached: it makes two LLM calls, and
        _finish_task is on the path that reports task completion to the UI.
        """
        from server import auto_memory

        try:
            n = auto_memory.append_history(eid, who, status, text)
        except Exception:
            logger.exception("Failed to append history for eid %s", eid)
            return
        # `>=`, not `n % every == 0`. The counter is no longer guaranteed to land
        # on a multiple: a pass that finishes subtracts only the appends it
        # actually consumed, so a window can start at any remainder. Modulo then
        # meant a full unconsumed window sat waiting for the count to reach the
        # *next* multiple — 10 appends stuck at 11 needing 9 more, or forever if
        # the eid went quiet with daily consolidation off.
        if n >= auto_memory.consolidate_every():
            self._schedule_consolidate(eid, n)

    def _schedule_consolidate(self, eid: str, n: int) -> None:
        """Run consolidation detached from the finishing task's coroutine.

        Held in ``_consolidate_tasks`` for the same reason notifications are:
        a resume calls kill_task(cancel_existing=True), and a consolidation
        awaiting an LLM inside that coroutine would be cancelled mid-write.
        Also guards against overlap — the per-eid lock inside consolidate()
        would serialize them, but queueing N of them would keep firing LLM
        calls long after the window that triggered them.
        """
        # Reserved synchronously, before create_task. The reservation used to be
        # made inside run(), which does not start until the loop next yields —
        # so two completions crossing the threshold back to back both saw an
        # empty set, both queued, and the second re-consolidated the same history
        # behind the lock, bumping every entry's n a second time.
        if eid in self._consolidating:
            logger.info("eid %s already consolidating, skipping this trigger", eid)
            return
        self._consolidating.add(eid)

        async def run() -> None:
            from server import auto_memory
            from server.routes_ws import _eid_tasks

            consumed_window = False
            try:
                tasks = _window_tasks(_eid_tasks(eid), n)
                logger.info(
                    "history reached %d entries for eid %s, consolidating %d tasks",
                    n, eid, len(tasks),
                )
                result = await auto_memory.consolidate(
                    eid, tasks, history_window_only=True)
                consumed_window = len(result["failed_layers"]) < 2
                logger.info(
                    "history-triggered consolidation done: eid=%s added=%d updated=%d "
                    "deleted=%d refused=%d%s",
                    eid, result["added"], result["updated"], result["deleted"],
                    result["refused"],
                    f" FAILED_LAYERS={result['failed_layers']}" if result["failed_layers"] else "",
                )
            except Exception:
                logger.exception("History-triggered consolidation failed for eid %s", eid)
            finally:
                self._consolidating.discard(eid)
            # Re-check for runs that finished during the two LLM calls: they bumped
            # the counter after this pass took its snapshot, and their own trigger
            # was dropped by the guard above — so without this they wait for the
            # next append, or forever if the eid goes quiet with daily
            # consolidation disabled.
            #
            # Keyed on the *minimum* pending across layers, not the maximum.
            # Since window accounting went per layer, a partial failure (lessons
            # succeeds, project times out) leaves project's pending count at the
            # threshold forever, and the maximum can no longer tell "new runs
            # arrived" from "a failed layer still owes this window". Re-arming on
            # the latter spins: the succeeded layer has no signals and returns
            # success without an LLM call, the failed one fails again, repeat —
            # hammering the broken helper with no new history. The minimum is what
            # every layer still owes, which only a genuine arrival can raise. A
            # failed layer's retry belongs to the next append or the nightly run.
            if not consumed_window:
                return
            try:
                left = auto_memory.min_pending(eid)
            except Exception:
                logger.exception("Failed to re-read history counter for eid %s", eid)
                return
            if left >= auto_memory.consolidate_every():
                logger.info(
                    "eid %s accumulated %d more entries while consolidating, "
                    "re-arming", eid, left,
                )
                self._schedule_consolidate(eid, left)

        t = asyncio.create_task(run(), name=f"consolidate-{eid}")
        self._consolidate_tasks.add(t)
        t.add_done_callback(self._consolidate_tasks.discard)

    def _prepare_notify(self, task_id: str, task: Task) -> tuple[str, Task, int] | None:
        """Build the (agent_name, task_snapshot, start_index) args for
        _schedule_notify, or None if notifications are disabled.

        Checks config before the deep copy so a disabled (default) setup
        never pays for copying a potentially large transcript.
        """
        from server.config import task_notify_config
        if not task_notify_config()["feishu_notify"].get("enabled"):
            return None
        agent = app_state.get_agent(task.agent_id)
        return (
            agent.name if agent else task.agent_id,
            task.model_copy(deep=True),
            self._run_start_index.get(task_id, 0),
        )

    def _schedule_notify(self, agent_name: str, task_snapshot: Task, start_index: int) -> None:
        """Fire the Feishu notification on a task detached from the runner's
        cancellable subprocess lifecycle, so a resume's cancel_existing=True
        can't cut off the CLI call mid-send."""
        from server.task_notify import notify_task_finished

        t = asyncio.create_task(notify_task_finished(agent_name, task_snapshot, start_index))
        self._notify_tasks.add(t)
        t.add_done_callback(self._notify_tasks.discard)

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
            # Only mark as resuming if there's an actual live run to kill —
            # user_message's handler (routes_ws.py) calls send_input() for
            # BOTH a brand new task's first message and a follow-up resume,
            # always with the default kill_existing=True. For a brand new
            # task there is no previous run, so unconditionally adding to
            # _resuming here would leave a stale entry that suppresses the
            # very first _finish_task(failed) call for THIS run (e.g. if
            # shutdown() cancels it while it's still in pre-spawn setup),
            # leaving the task stuck at running with no completion card.
            if task_id in self._subprocess_tasks:
                # Mark as resuming so the dying subprocess doesn't overwrite status with failed
                self._resuming.add(task_id)
            # Kill current process if still running
            await self.kill_task(task_id)
            # Close whatever the superseded run left open. Its _finish_task
            # takes the _resuming early return — which exists to protect the
            # task *status*, not to skip cleanup — so without this the old
            # bubble stays streaming=True in the UI and in the saved transcript
            # until some later run happens to finish. Done here, before the
            # replacement starts, so every open bubble provably belongs to the
            # run being replaced.
            await self._close_open_bubbles(task_id)

        # Start a new subprocess resuming the session
        self._start_subprocess(task_id, user_input, cancel_existing=kill_existing)

    # ── kill ────────────────────────────────────────────────────────────

    @staticmethod
    def _record_retained_pgid(
        task_id: str, pgid: int, leader_start: int | None
    ) -> None:
        """记下一个"没能证明已消失"的进程组，与 subprocess_pid 分开存。

        subprocess_pid 描述的是"当前这一轮跑的是谁"，resume 会理所当然地覆盖它。
        但一个扛过 SIGKILL、或身份始终核验不出来的组，可能还握着 codex 的 writer
        lock；覆盖之后它就没有任何把手了。retained_pgids 是一份 append-only 的
        (pgid, leader_start) 列表，只由 restore_orphan_tasks 在证明其消失后摘除。
        """
        task = app_state.get_task(task_id)
        if task is None:
            return
        retained = list(getattr(task, "retained_pgids", None) or [])
        entry = [pgid, leader_start]
        # 同一个组反复 kill 不该堆重复项。身份一起比，被回收的号是不同的条目。
        if entry not in retained:
            retained.append(entry)
            object.__setattr__(task, "retained_pgids", retained)
            app_state.save_agent_tasks(task.agent_id)

    async def kill_task(self, task_id: str) -> None:
        """Terminate subprocess for a task."""
        # Note: _compact_warned is intentionally NOT cleared here. send_input()
        # calls kill_task() on every resume (default kill_existing=True), which
        # would defeat the one-shot compact warning. Real teardown paths
        # (routes_rest.delete_task) clear the flag via forget_task().
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

        # Pipe mode: kill the whole process group, not just the direct child.
        # proc.terminate() alone left grandchildren running (see the
        # start_new_session comment in _run_pipe_mode), and a survivor holding
        # codex's thread writer lock makes every later resume fail. Signal by
        # pgid first and fall back to the bare pid if the group is already gone.
        proc = self._async_procs.pop(task_id, None)
        leader_start = self._proc_starts.pop(task_id, None)
        if proc is not None:
            # Not gated on proc.returncode: the bug being fixed here is exactly
            # a dead wrapper whose descendants outlived it, and those orphans
            # stay in the group even after the direct child is reaped. That also
            # means the pid may already be free, so each delivery is gated on the
            # leader's recorded start time — otherwise dropping the returncode
            # guard would let a reused pgid absorb this kill.
            state = pg.GROUP_UNKNOWN
            for sig in (signal.SIGTERM, signal.SIGKILL):
                sent = pg.killpg_verified(proc.pid, leader_start, sig)
                if not sent and proc.returncode is None:
                    # Group unreachable or no longer ours, but our own direct
                    # child is provably still alive — signal it by handle, which
                    # cannot be confused by pid reuse.
                    try:
                        if sig == signal.SIGTERM:
                            proc.terminate()
                        else:
                            proc.kill()
                    except ProcessLookupError:
                        pass
                if sig == signal.SIGTERM:
                    await asyncio.sleep(0.5)
                    # Escalate on the process group, not on proc.returncode: a
                    # wrapper that exits promptly on SIGTERM says nothing about a
                    # descendant that ignored it, and that descendant is exactly
                    # the writer-lock holder this kill exists to remove.
                    #
                    # Only pg.GROUP_GONE stops the escalation. pg.GROUP_UNKNOWN means
                    # we could not verify, which is not evidence of death —
                    # breaking on it would skip SIGKILL on a live descendant.
                    # pg.GROUP_FOREIGN means the pgid is someone else's now, so
                    # there is nothing of ours left to escalate against.
                    state = pg.group_state(proc.pid, leader_start)
                    if state in (pg.GROUP_GONE, pg.GROUP_FOREIGN):
                        break
                else:
                    # SIGKILL 也不是同步的：卡在不可中断等待里的成员会让它挂起。
                    # 和 restore_orphan_tasks 一样先轮询等一小会儿，再下最终判决，
                    # 否则一个其实马上就会死的组会被误判成"扛过了 SIGKILL"。
                    for _ in range(10):
                        state = pg.group_state(proc.pid, leader_start)
                        if state in (pg.GROUP_GONE, pg.GROUP_FOREIGN):
                            break
                        await asyncio.sleep(0.1)
            # 最终判决：只有 gone/foreign 才算"我们这边已经没有活口"。
            # pg.GROUP_OURS 表示组扛过了 SIGKILL，pg.GROUP_UNKNOWN 表示两次投递都被闸门
            # 拒绝（/proc 一直读不全），两者都不是死亡证据 —— 而直接子进程可能已经
            # 因为 handle 版 SIGTERM 退出，pgid 就成了残留后代（codex writer lock 的
            # 持有者）唯一的把手。标记一下，别让 _cleanup_run_resources 把它清掉。
            if state not in (pg.GROUP_GONE, pg.GROUP_FOREIGN):
                self._retain_pid.add(task_id)
                # subprocess_pid 只够撑到本次 cleanup：send_input 默认 kill 完立刻
                # 起新进程，spawn 会把它覆盖成新 pid，这个也许还活着的旧组就再也
                # 找不回来了（新 run 若同样卡在 writer lock 上失败，它的 cleanup
                # 清掉的是新 pid）。所以另存一份带身份的把手，与当前 run 无关。
                self._record_retained_pgid(task_id, proc.pid, leader_start)
                logger.error(
                    "Group pgid=%d (task %s) not verifiably gone after kill_task; "
                    "retaining pid metadata for startup retry",
                    proc.pid, task_id,
                )
            # Reap the process so it does not linger as a zombie holding the
            # asyncio transport open.
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
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

    # ── task teardown (delete path) ─────────────────────────────────────

    def forget_task(self, task_id: str) -> None:
        """Clear per-task bookkeeping that must survive resumes but not deletion.

        Call from real teardown paths (e.g. routes_rest.delete_task) after
        kill_task. Not called from send_input's kill_task — we want the
        compact-warning one-shot flag to survive normal resumes.
        """
        self._compact_warned.discard(task_id)
        self._compact_pending.discard(task_id)
        self._handoff_pending.discard(task_id)
        # 任务已被删除，没有下一次启动可以重试它的组了。
        self._retain_pid.discard(task_id)
        # 同理，retained_pgids 会随任务一起消失，所以在丢掉把手前用掉它：闸门保证
        # 只有仍属于我们的组会收到信号，被回收的号不会被误杀。不等待确认 —— 这里是
        # 请求线程，删除路径也没有下一次可以重试了。
        task = app_state.get_task(task_id)
        for pgid, leader_start in list(getattr(task, "retained_pgids", None) or []):
            pg.killpg_verified(pgid, leader_start, signal.SIGKILL)
        self._run_start_index.pop(task_id, None)
        self._input_locks.pop(task_id, None)
        # Cleared here, not in _cleanup_run_resources: the snapshot must outlive
        # every run of this task so resumes keep sending identical bytes.
        self._memory_snapshots.pop(task_id, None)

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
            # Also revisit already-failed tasks that still carry pid metadata.
            # A group that outlives SIGKILL keeps its pid recorded precisely so a
            # later startup can retry; without this clause that retry never
            # happens, since the task was marked failed on the way out and the
            # status filter alone would skip it forever.
            #
            # Restricted to `failed`, not "anything not running": _finish_task
            # persists a successful status before _cleanup_run_resources clears
            # and persists the pid, so a crash in that window leaves a genuinely
            # successful task holding a stale pid. Admitting it here would
            # rewrite a completed result to failed on the next startup.
            retry_pid = (
                task.status == TaskStatus.failed
                and getattr(task, "subprocess_pid", None) is not None
            )
            if task.status.value not in ("running", "waiting") and not retry_pid:
                continue
            pid = getattr(task, "subprocess_pid", None)
            expected_start_time = getattr(task, "subprocess_start_time", None)
            task_id = task.id
            if pid is None:
                # No PID persisted — the task was started in a previous
                # server version or never had one.  Mark as failed.
                task.status = TaskStatus.failed
                object.__setattr__(task, "subprocess_start_time", None)
                logger.info(
                    "Orphan task %s has no PID, marking as failed", task_id
                )
                app_state.save_agent_tasks(task.agent_id)
                cleaned.append(task_id)
                continue

            # Validate PID identity before signaling. PID values can be
            # recycled after server restarts.
            actual_start_time = pg.read_proc_start_time(pid)
            if (
                expected_start_time is None
                or actual_start_time is None
                or str(actual_start_time) != str(expected_start_time)
            ):
                # The leader is gone, but the group it created can outlive it —
                # that orphaned-descendant shape is precisely what holds codex's
                # thread writer lock and makes every later resume fail. The
                # recorded pid doubles as the pgid (both the PTY path's setsid
                # child and pipe mode's start_new_session child lead their own
                # group), so surviving members stay addressable with no leader
                # to identify. Signal them individually: killpg would need the
                # group to still exist as such, and we do not trust `pid` here.
                #
                # Gated on pg.pid_is_absent, NOT on actual_start_time being None.
                # A None start time also covers a transient stat read/parse
                # failure on a pid that still exists, and a live stranger that
                # called setsid leads a group whose pgid equals that same pid —
                # signaling it would kill an unrelated tree.
                #
                # ENOENT is only a point-in-time observation, so it is checked
                # again after enumeration: nothing stops the kernel from handing
                # this number out in between, and a new leader claiming it would
                # create a group with this very pgid that pg.pgroup_member_ids
                # would happily return. Absent before AND after means no such
                # window opened. Members carry their start times so the kill
                # itself is identity-checked too (see pg.kill_verified).
                survivors: list[tuple[int, int | None]] = []
                # "扫不全" 必须传递到下面的无幸存者分支：此处 leader 已缺席，
                # 那条分支的保留条件全靠 pid 还在，判不出这种情况，元数据会被
                # 直接清掉，而没被看见的后代及其 writer lock 就永远回收不了。
                scan_incomplete = False
                if pg.pid_is_absent(pid):
                    survivors, scan_complete = pg.scan_pgroup(pid)
                    if not pg.pid_is_absent(pid):
                        logger.warning(
                            "Orphan task %s pid=%d was re-allocated while enumerating its "
                            "group; skip signaling to avoid killing an unrelated tree",
                            task_id, pid,
                        )
                        survivors = []
                    elif not scan_complete:
                        logger.warning(
                            "Orphan task %s pid=%d group scan was incomplete; skip "
                            "signaling this round",
                            task_id, pid,
                        )
                        survivors = []
                        scan_incomplete = True
                if survivors:
                    logger.warning(
                        "Orphan task %s leader pid=%d is gone but %d process(es) remain in "
                        "its group; signaling them directly",
                        task_id,
                        pid,
                        len(survivors),
                    )
                    for member, member_start in survivors:
                        pg.kill_verified(member, member_start)
                    # Re-enumerate rather than assume the kills took: a member
                    # wedged uninterruptibly keeps SIGKILL pending, and one
                    # forked after enumeration was never signaled at all. If any
                    # remain — or if the scan could not see all of /proc — keep
                    # the pgid, it is the only handle on them.
                    still, complete = pg.scan_pgroup(pid)
                    if still or not complete or not pg.pid_is_absent(pid):
                        logger.error(
                            "Orphan task %s group pgid=%d not provably empty after "
                            "verified kills (members=%d complete=%s); retaining pid "
                            "metadata for a later retry",
                            task_id, pid, len(still), complete,
                        )
                        task.status = TaskStatus.failed
                        app_state.save_agent_tasks(task.agent_id)
                        cleaned.append(task_id)
                        continue
                else:
                    # No survivors to signal. Distinguish "the pid is genuinely
                    # gone / re-leased" from "we could not read it": the latter
                    # may still be our live group, and clearing the pid here
                    # would strip the failed-task retry of the only handle it
                    # has, leaving the writer lock held forever.
                    #
                    # 三种 "读不出来" 都要保留元数据，它们都不是 "已消失" 的证据：
                    #   1. 组扫描不完整 —— 空成员列表只代表没看全，不代表组为空；
                    #   2. pid 还在但当前身份读不出来 —— 可能就是我们的组；
                    #   3. pid 还在但记录的身份缺失（启动时 pg.read_proc_start_time
                    #      瞬时失败，只持久化了 subprocess_pid）—— 缺的是比对基准，
                    #      不是身份校验失败，同样无权判定这个活着的进程与我们无关。
                    pid_held = not pg.pid_is_absent(pid)
                    if scan_incomplete or (
                        pid_held
                        and (actual_start_time is None or expected_start_time is None)
                    ):
                        logger.error(
                            "Orphan task %s pid=%d identity/group could not be verified "
                            "(scan_incomplete=%s expected_start=%s actual_start=%s); "
                            "retaining pid metadata for a later retry",
                            task_id, pid, scan_incomplete,
                            expected_start_time, actual_start_time,
                        )
                        task.status = TaskStatus.failed
                        app_state.save_agent_tasks(task.agent_id)
                        cleaned.append(task_id)
                        continue
                    logger.warning(
                        "Orphan task %s pid identity check failed (pid=%d expected_start=%s actual_start=%s); "
                        "skip signaling and mark failed",
                        task_id,
                        pid,
                        expected_start_time,
                        actual_start_time,
                    )
                task.status = TaskStatus.failed
                object.__setattr__(task, "subprocess_pid", None)
                object.__setattr__(task, "subprocess_start_time", None)
                app_state.save_agent_tasks(task.agent_id)
                cleaned.append(task_id)
                continue

            # Kill the surviving process — we've lost the PTY fd and
            # cannot recover the I/O channel.
            #
            # Escalate to SIGKILL before clearing subprocess_pid below: this is
            # the last moment we hold the group's identity, so a worker that
            # ignores SIGTERM would otherwise keep codex's writer lock forever
            # with no metadata left to find it by. Every delivery re-verifies the
            # leader identity: the check above establishes ownership only at that
            # instant, and the group can exit and have its pgid re-leased during
            # the waits below.
            final_state = pg.terminate_group_blocking(pid, expected_start_time)

            # Also reap zombie children
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            except Exception:
                pass

            task.status = TaskStatus.failed
            if final_state in (pg.GROUP_OURS, pg.GROUP_UNKNOWN):
                # Keep subprocess_pid/start_time: the group outlived SIGKILL (or
                # could not be verified at all), so the next startup should get
                # another chance at it rather than inherit a task marked failed
                # with no way to find its process. Retaining on UNKNOWN is the
                # conservative half of that: a stale pid costs one extra check
                # next boot, a discarded live one costs a permanent writer lock.
                logger.error(
                    "Orphan group pgid=%d (task %s) is %s after SIGKILL; retaining pid "
                    "metadata so a later restart can retry",
                    pid, task_id, final_state,
                )
            else:
                object.__setattr__(task, "subprocess_pid", None)
                object.__setattr__(task, "subprocess_start_time", None)
            logger.info(
                "Orphan task %s (pid=%d) terminated and marked as failed",
                task_id, pid,
            )
            app_state.save_agent_tasks(task.agent_id)
            cleaned.append(task_id)
        # 扫一遍 retained_pgids：这些是历次 kill_task 没能证明消失、又被后续 resume
        # 的新 pid 从 subprocess_pid 里挤掉的旧组。它们与任务当前状态无关（任务可能
        # 已经成功跑完好几轮了），所以独立成一趟，只按身份核验、按身份收割。
        for task in list(app_state.tasks.values()):
            retained = list(getattr(task, "retained_pgids", None) or [])
            if not retained:
                continue
            survivors: list[list] = []
            for pgid, leader_start in retained:
                state = pg.terminate_group_blocking(pgid, leader_start)
                if state in (pg.GROUP_GONE, pg.GROUP_FOREIGN):
                    logger.info(
                        "Retained group pgid=%d (task %s) is %s; dropping the handle",
                        pgid, task.id, state,
                    )
                    continue
                # 仍是 OURS/UNKNOWN：没有死亡证据，把手必须留到下次启动。
                logger.error(
                    "Retained group pgid=%d (task %s) is %s after SIGKILL; keeping the "
                    "handle for a later restart",
                    pgid, task.id, state,
                )
                survivors.append([pgid, leader_start])
            if survivors != retained:
                object.__setattr__(task, "retained_pgids", survivors)
                app_state.save_agent_tasks(task.agent_id)
        # Restore persisted auto-compact opt-outs across all tasks
        for task in app_state.tasks.values():
            if getattr(task, "auto_compact_disabled", False):
                self._auto_compact_disabled.add(task.id)
            if getattr(task, "plan_mode", False):
                self._plan_mode.add(task.id)
        return cleaned


    async def shutdown(self) -> None:
        """Graceful shutdown: kill any tracked subprocesses."""
        loop = asyncio.get_event_loop()
        # 一个统管全局的截止时间，而不是每阶段各自计时后相加：SIGTERM/SIGKILL 两段在
        # 保留组仍为 OURS/UNKNOWN 时会把 15s 全部用满，后面的通知 drain（最多 80s）与
        # consolidation drain/cancel（5+8s）再叠上去就超出 run.sh 的 95s grace，届时
        # 后端被 kill -9，通知卡与 helper 进程的收尾都不会跑。下面每一处等待都按
        # _budget_left() 收敛，谁先用掉预算谁就压缩后面的窗口。
        budget_deadline = loop.time() + _shutdown_total_budget_seconds()

        def _budget_left(cap: float, reserve: float = 0.0) -> float:
            """*cap* 与剩余总预算取小，且不为负（asyncio.wait 不接受负 timeout）。

            *reserve* 是要留给后续阶段的秒数：靠前的等待（尤其能吃掉 80s 的通知
            drain）不能把预算耗光，否则最后那段 cancel-and-reap 拿到 0s，glm/cco
            helper 就在后端退出后成了孤儿 —— 那正是这段预算要防的事。
            """
            return max(0.0, min(cap, budget_deadline - loop.time() - reserve))
        # Process groups we have signaled, tracked outside _live_runs so a group
        # whose entry disappears mid-drain still gets escalated. See the comment
        # at the .add() below.
        #
        # 键是 (pgid, leader_start) 整体，不是裸 pgid：kill_existing=False 的续话可以在
        # 上一个 run 还在 finalize 时启动，若旧 pid/pgid 已被回收复用，_live_runs 里就同
        # 时存在两条 pid 相同、pid_start 不同的记录。按裸 pgid 记的话先来的身份占坑，新
        # run 的投递被闸门正确地判成 foreign 拒发（且每轮又被重新装回旧身份），它的后代
        # 一个信号都收不到 —— 与第 13 轮 retained 集合改成按身份存是同一个道理。
        #
        # 值是 task_id：见下面 _persist_unresolved()，把手要写回哪个任务的元数据全靠它。
        signaled_pgids: dict[tuple[int, int | None], str | None] = {}

        def _prune_signaled() -> None:
            """只摘掉拿到确定判决（GONE/FOREIGN）的组。

            UNKNOWN 不是死亡证据（见 pg.group_state），把它当成消失就等于放走一个可能还
            活着的后代。留在集合里的组即意味着"还有 OURS/UNKNOWN 要继续等/复检"。
            """
            for entry in list(signaled_pgids):
                if pg.group_state(*entry) in (pg.GROUP_GONE, pg.GROUP_FOREIGN):
                    signaled_pgids.pop(entry, None)

        # kill_task 判决"没能证明消失"的旧组：后续 resume 已经把 subprocess_pid 换成
        # 新 pid，它们既不在 _live_runs 里，也不在上面这份 signaled_pgids 里，唯一的
        # 把手是任务元数据的 retained_pgids。只靠 restore_orphan_tasks 收割意味着
        # `run.sh stop`（后面不接 start）会在这些组仍握着 codex writer lock 时退出
        # 后端，所以这里一起 drain，走同一套身份闸门。
        #
        # 与 signaled_pgids 分开存：某个旧 pgid 可能已被我们的新 run 复用，混进同一份
        # 映射会让那条 live run 拿着旧身份去投递（闸门正确地拒发 → 组里的后代反而一个
        # 信号都收不到）。
        #
        # 以 (pgid, leader_start) 整体为元素，而不是 pgid → 身份的映射：同一个号可以在
        # 元数据里出现多次（旧组尚未被证明消失，号已被后来的 run 复用，两条记录都要留到
        # 下次启动才复核）。只保留先记下的那个身份会让另一个身份的组永远收不到信号 ——
        # 前者每轮被判 FOREIGN 剔掉、下一轮又从元数据里被加回来，后者始终进不了集合。
        # 每个身份各占一条，该不该投递交给闸门按身份逐条判。
        retained_pgids: set[tuple[int, int | None]] = set()

        def _refresh_retained() -> None:
            # 每一趟都重扫任务元数据：drain 期间仍可能有 kill_task 落地并记下新把手。
            for t in list(app_state.tasks.values()):
                for pgid, leader_start in (getattr(t, "retained_pgids", None) or []):
                    retained_pgids.add((pgid, leader_start))
            for entry in list(retained_pgids):
                if pg.group_state(*entry) in (pg.GROUP_GONE, pg.GROUP_FOREIGN):
                    # 只有确定的判决才移出这份集合；UNKNOWN 不是死亡证据。
                    retained_pgids.discard(entry)

        def _signal_retained(sig: int) -> None:
            _refresh_retained()
            for pgid, leader_start in list(retained_pgids):
                pg.killpg_verified(pgid, leader_start, sig)
            # 不在这里改写 retained_pgids 元数据：把手的生命周期只有
            # restore_orphan_tasks 一个 owner（见 _record_retained_pgid），它在下次
            # 启动时按身份复核后摘除。多留一轮的代价只是下次启动多一次核验。

        def _persist_unresolved() -> None:
            """把 shutdown 结束时仍未判定消失的组写进任务的 retained_pgids。

            这是刻意打破"只有 restore_orphan_tasks 能写把手"的一处例外，因为不写就没有
            把手了：signaled_pgids 是 shutdown 的局部变量，随进程一起消失；而这些组的另
            一份把手 subprocess_pid 会被它们自己那条 run 的 _cleanup_run_resources 清掉
            —— shutdown 既没有走 kill_task，也就没进 _retain_pid，那条保留逻辑不生效。
            于是 `run.sh restart` 之后，一个仍握着 codex writer lock 的后代对新后端完全
            不可见。

            只写 _prune_signaled 之后还剩下的条目，即判决为 OURS/UNKNOWN 的组；拿到
            GONE/FOREIGN 的已经被摘掉，不会被误记。多记一条活把手的代价只是下次启动多一
            次按身份的核验（同一身份重复记不会堆项，见 _record_retained_pgid），而
            restore_orphan_tasks 只在证明其消失后才摘除 —— 永远不会误删活把手。
            """
            _prune_signaled()
            for (pgid, leader_start), task_id in signaled_pgids.items():
                if task_id is None:
                    logger.error(
                        "Group pgid=%d not verifiably gone at shutdown and has no owning "
                        "task; no handle survives this exit",
                        pgid,
                    )
                    continue
                logger.error(
                    "Group pgid=%d (task %s) not verifiably gone at shutdown; persisting "
                    "the handle so the next startup can retry",
                    pgid, task_id,
                )
                self._record_retained_pgid(task_id, pgid, leader_start)

        def _run_identity(run: dict, pid: int) -> tuple[int, int | None]:
            """(pgid, leader_start)，spawn 时身份读失败的话补读一次并回写。

            回写是关键：补读只在"子进程尚未被回收"的窗口内有效，而 shutdown 会把这两个
            阶段的循环各跑很多轮 —— 不缓存的话，等收割线程 done 之后再补读就永远失败，
            同一个组在 signaled_pgids 里又多出一条 None 身份的条目（闸门永远拒发它，
            _prune_signaled 也永远摘不掉它，SIGKILL 阶段因此空转到预算耗尽）。
            """
            leader_start = run.get("pid_start")
            if leader_start is None:
                leader_start = pg.recapture_leader_start(pid, pg.run_child_unreaped(run))
                if leader_start is not None:
                    run["pid_start"] = leader_start
            return (pid, leader_start)

        def _sigterm_all() -> None:
            # Iterate _live_runs (keyed by run_id, never overwritten by an
            # unrelated run) rather than the task_id-keyed _pids/_async_procs
            # — a kill_existing=False continuation started for the same
            # task_id while an older run is still finishing would otherwise
            # make that older run's pid/proc invisible here.
            for run in list(self._live_runs.values()):
                pid = run.get("pid")
                group_signaled = False
                if pid is not None:
                    # Remember the group independently of _live_runs. _on_done
                    # drops the entry as soon as _run_pipe_mode returns, and it
                    # returns on stdout EOF — which a SIGTERM-resistant
                    # descendant can trigger just by closing or redirecting
                    # stdout while it keeps running. The group would then be
                    # forgotten before the SIGKILL phase and keep its writer
                    # lock; group liveness, not entry lifetime, decides when we
                    # are done with it.
                    #
                    # Prefer the identity recorded at spawn (_run_pipe_mode) over
                    # a read taken now: by this point the leader may already be
                    # reaped, and reading then would either get None or, worse,
                    # a new holder's start time — baking the impostor's identity
                    # into the map as though it were ours.
                    entry = (pid, run.get("pid_start"))
                    if entry[1] is None:
                        # spawn 时那一次 /proc 读失败（PTY 路径尤其没有 proc 句柄可以
                        # 兜底）。只要子进程还没被回收，号码就还被我们占着，补读到的
                        # 身份必然是我们那个 leader —— 补上闸门就能重新放行，而不是
                        # 让整个组从此收不到任何信号。补不到就仍然是 None（闸门继续
                        # 拒发），下面按自家子进程直投。
                        entry = _run_identity(run, pid)
                    signaled_pgids.setdefault(entry, run.get("task_id"))
                    group_signaled = pg.killpg_verified(*entry, signal.SIGTERM)
                proc = run.get("proc")
                if proc is not None and proc.returncode is None and (
                    pid is None or not group_signaled
                ):
                    # Normally the killpg above covers pipe-mode runs, reaching
                    # their grandchildren as proc.terminate() cannot. But if it
                    # declined (unverifiable identity, or a re-leased pgid), our
                    # own direct child would otherwise get no signal at all —
                    # and signaling by handle cannot hit an unrelated process.
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                elif proc is None and pid is not None and not group_signaled:
                    # PTY 路径的等价兜底。它没有 proc 句柄，闸门拒发后原来只剩关
                    # transport（那只让读循环收尾，进程组照样活着）—— `run.sh
                    # stop/restart` 会把这个组永久留在机器上。按 ppid 核过再投，
                    # 与 handle 版一样不可能打到陌生进程。
                    pg.kill_direct_child(pid, signal.SIGTERM)

        _sigterm_all()
        _signal_retained(signal.SIGTERM)

        # Let killed runs reach their own finalization (_finish_task, which
        # schedules the completion card) before we snapshot _notify_tasks below.
        # Loop rather than snapshot-once: a run that finishes cleanly on its
        # own (not killed by the SIGTERM above) can dispatch a kill_existing=
        # False continuation (max-turns auto-resume, successful auto-compact),
        # registering a brand new _live_runs entry after we've already taken
        # our snapshot. Repeatedly re-snapshotting and SIGTERM'ing any
        # newcomers until the set actually drains (or the overall budget below
        # runs out) keeps such continuations from being silently orphaned.
        deadline = loop.time() + _budget_left(_sigterm_grace_seconds())
        # signaled_pgids 也要进循环条件（与下面 SIGKILL 阶段的复检循环对称）：wrapper 收
        # 到 SIGTERM 后立刻退出、后代把 stdout 关掉但仍在 graceful cleanup 时，_on_done
        # 会把唯一那条 _live_runs 记录摘掉 —— 组只剩 signaled_pgids 这一份把手（它既不属
        # 于任何 live run，也不在 retained_pgids 里，那份只装 kill_task 记下的旧组）。
        # 只看 _live_runs/retained_pgids 的话条件立刻为假，shutdown 直接跳到 SIGKILL，
        # 说好的 10s graceful 窗口那个后代一秒都没拿到，收尾写一半就被打断。
        _prune_signaled()
        while (
            (self._live_runs or retained_pgids or signaled_pgids)
            and loop.time() < deadline
        ):
            _sigterm_all()  # catch pids/procs registered by new continuations
            _signal_retained(signal.SIGTERM)
            # 每轮开头复核：拿到 GONE/FOREIGN 的号摘掉，否则一个早就退干净的组会把整个
            # graceful 窗口耗满（原来靠 _live_runs 变空来收敛）。
            _prune_signaled()
            tasks = [r["task"] for r in self._live_runs.values() if r.get("task")]
            if not tasks:
                if not retained_pgids and not signaled_pgids:
                    break
                # 保留组/独立跟踪的组没有任何 asyncio task 可以 await（它们本来就不属于哪
                # 条 live run），但"没东西可等"不等于"可以立刻升级"。按短间隔轮询到
                # deadline，它们才真正拿到一段 graceful SIGTERM 窗口；否则 _live_runs 一
                # 空，下面就在同一个事件循环回合里 SIGKILL 了它。
                await asyncio.sleep(min(0.2, max(0.0, deadline - loop.time())))
                continue
            # Poll on a short interval rather than waiting the full remaining
            # budget: asyncio.wait's default ALL_COMPLETED means a single
            # still-running task (e.g. a PTY reader blocked on a lingering
            # grandchild) makes this call block for its entire timeout, so a
            # continuation registered moments later never gets re-snapshotted
            # or SIGTERM'd until the whole budget is already gone.
            await asyncio.wait(tasks, timeout=min(1.0, max(0.0, deadline - loop.time())))

        # Anything not provably finished keeps the escalation phase alive.
        # pg.GROUP_UNKNOWN counts: it means we could not verify, not that the group
        # died, and excluding it would skip SIGKILL on a live descendant.
        # _prune_signaled 只摘掉 GONE/FOREIGN，所以剩下的就是 OURS/UNKNOWN。
        _prune_signaled()
        # 保留组同样要能把 SIGKILL 阶段拉起来：SIGTERM 之后它们可能仍是 OURS/UNKNOWN，
        # 而 _live_runs 与 signaled_pgids 都可能已经空了（这些组本来就不属于任何 live
        # run）。_refresh_retained 顺便剔掉已有确定判决的条目。
        _refresh_retained()
        if self._live_runs or signaled_pgids or retained_pgids:
            # SIGTERM didn't finish the job in time — escalate to SIGKILL
            # and give finalization a second, shorter window. run.sh's
            # stop grace was sized to cover this (see do_stop). Without
            # this, a child that ignores/delays SIGTERM would never reach
            # _finish_task, leaving its task stuck at running with no
            # completion card and no exit.
            def _sigkill_all() -> None:
                # 先打保留组：它们没有 live run 兜底，_live_runs 空掉后下面的循环就不
                # 再跑，这里是它们唯一的 SIGKILL 来源。
                _signal_retained(signal.SIGKILL)
                # Groups first, so one whose _live_runs entry already went away
                # (stdout closed by a still-running descendant) is still killed.
                # Drop only pgids with a definite death/foreign verdict: an
                # unverifiable one may still be alive, and forgetting it here is
                # how a live descendant escapes the escalation entirely.
                _prune_signaled()
                for entry in list(signaled_pgids):
                    pg.killpg_verified(*entry, signal.SIGKILL)
                for run in list(self._live_runs.values()):
                    pid = run.get("pid")
                    group_signaled = False
                    if pid is not None:
                        # Same identity gate as _sigterm_all: a run still in
                        # finalization keeps its entry here after its group has
                        # exited, so this delivery is as exposed to pid reuse as
                        # the tracked-pgid loop above.
                        entry = (pid, run.get("pid_start"))
                        if entry[1] is None:
                            # 同 _sigterm_all：spawn 时读失败不该让这一组永久过不了
                            # 闸门。子进程未回收 ⇒ 号码还是我们的 ⇒ 补读的身份可用。
                            entry = _run_identity(run, pid)
                        signaled_pgids.setdefault(entry, run.get("task_id"))
                        group_signaled = pg.killpg_verified(*entry, signal.SIGKILL)
                    proc = run.get("proc")
                    if proc is not None and proc.returncode is None and (
                        pid is None or not group_signaled
                    ):
                        # See _sigterm_all: fall back to the handle whenever the
                        # group kill did not land, not only when no pid exists.
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                    elif proc is None and pid is not None and not group_signaled:
                        # PTY 路径：闸门拒发时唯一还能落地的一发 SIGKILL。关 transport
                        # 只是让读循环退出，杀不掉任何进程。
                        pg.kill_direct_child(pid, signal.SIGKILL)
                    # SIGKILL to the recorded pid/pgid can't reach a detached
                    # grandchild that still holds the pty slave fd open — that
                    # case only unblocks via the internal 60s lingering-writer
                    # timer (see _run_pty_mode), which is longer than this
                    # shutdown's own bounded wait. Close the transport directly
                    # so the pending _read_json_lines loop sees EOF/error now
                    # instead of stranding this shutdown past run.sh's kill deadline.
                    transport = run.get("transport")
                    if transport is not None:
                        transport.close()

            # Loop here too (mirrors the SIGTERM phase above): a run still
            # inside result handling when the SIGTERM deadline hit (e.g.
            # awaiting a bounded broadcast) can still dispatch a
            # kill_existing=False continuation during this escalation window,
            # registering yet another _live_runs entry that a one-shot
            # snapshot+wait would silently miss.
            kill_deadline = loop.time() + _budget_left(_sigkill_grace_seconds())
            # Unconditional first pass: the loop below is gated on _live_runs,
            # which can already be empty here when the only thing left is a
            # lingering process group (its run entry dropped on stdout EOF).
            _sigkill_all()
            # signaled_pgids 也要进循环条件：wrapper 在 SIGTERM 之后退出、_on_done 把
            # run 移出 _live_runs 时，一个仍活着的后代只剩这一份把手 —— 它既不在
            # _live_runs 里，也不在 retained_pgids 里（那份只装 kill_task 记下的旧组）。
            # 只投一发 SIGKILL 就返回，等于不核验组是否真的消失，暂时卡在不可中断等待里
            # 的成员就活过了 `run.sh stop`。_sigkill_all() 每轮开头已把拿到确定判决
            # （GONE/FOREIGN）的号摘掉，所以这里非空即意味着还有 OURS/UNKNOWN 要复检。
            while (
                (self._live_runs or retained_pgids or signaled_pgids)
                and loop.time() < kill_deadline
            ):
                _sigkill_all()
                tasks = [r["task"] for r in self._live_runs.values() if r.get("task")]
                if not tasks:
                    if not retained_pgids and not signaled_pgids:
                        break
                    # 同 SIGTERM 阶段：保留组没有可 await 的 task，但 SIGKILL 也不是
                    # 同步的 —— 卡在不可中断等待里的成员要过一会儿才真正消失。轮询到
                    # kill_deadline 再放手，否则投完信号就返回，run.sh stop 之后那个组
                    # 可能还握着 codex writer lock 活着。
                    await asyncio.sleep(min(0.2, max(0.0, kill_deadline - loop.time())))
                    continue
                # Same reasoning as the SIGTERM loop above: poll on a short
                # interval so a continuation registered mid-drain gets
                # re-snapshotted and SIGKILL'd instead of waiting out this
                # whole (already short) escalation budget unattended.
                await asyncio.wait(tasks, timeout=min(1.0, max(0.0, kill_deadline - loop.time())))

            if self._live_runs:
                # Some run(s) never spawned a child to signal — e.g. still
                # awaiting a pre-spawn step like search_wiki() (default
                # timeout 30s, longer than the 10s+5s SIGTERM/SIGKILL budget
                # above), so _sigkill_all() was a no-op for them. Their asyncio
                # coroutine is the only handle left; cancel it directly rather
                # than let lifespan's own event-loop teardown do it implicitly
                # (which would skip _finish_task and its failure notification).
                for run in list(self._live_runs.values()):
                    task_obj = run.get("task")
                    if task_obj and not task_obj.done():
                        task_obj.cancel()
                tasks = [r["task"] for r in self._live_runs.values() if r.get("task")]
                if tasks:
                    await asyncio.wait(tasks, timeout=_budget_left(2))

        # 进程组两阶段到此结束。仍未判定消失的组要在这里落盘：signaled_pgids 只活在这个
        # 函数的栈上，而它们的另一份把手 subprocess_pid 马上就会被各自 run 的
        # _cleanup_run_resources 清掉（shutdown 没走 kill_task，_retain_pid 那条保留逻辑
        # 不生效）。放在预算收敛之后、返回之前，等的都等过了，判决是最终的那一次。
        _persist_unresolved()
        # Give in-flight Feishu notifications a bounded window to finish before
        # the event loop closes and cancels them. send_feishu_card's own CLI
        # timeout is 30s (wiki_notify.py); this outer wait must exceed that
        # with real margin — asyncio.wait's timeout doesn't cancel the pending
        # task, so if both timers were equal and this one fired first,
        # shutdown would return with the notification's own timeout handler
        # (which kills its CLI child) never having run.
        #
        # Notifications for the SAME task coalesce rather than queue
        # (task_notify keeps at most one in flight plus one pending slot), so
        # the worst case is a FIXED MAX_SERIAL_SENDS CLI calls back to back.
        # The budget is that constant — not a sampled depth, which could miss a
        # coroutine scheduled but not yet started.
        if self._notify_tasks:
            # 只有真有 consolidation 在飞时才为它留额度，否则通知白白少等一截。
            # 无论如何都要为通知自己的 kill-and-reap 留一段：drain 被压缩时（17s 进程组
            # + 13s consolidation 之后只剩 60s，装不下 80s）超时的通知不会被 asyncio.wait
            # 取消，事件循环 teardown 会把它静默丢弃 —— 它的 except CancelledError 才是
            # 杀掉 feishu-bot CLI 子进程的地方，不跑就留下孤儿（run.sh 只 signal 后端 PID）。
            reserve = _notify_kill_seconds() + (
                _consolidate_drain_seconds() + _consolidate_kill_seconds()
                if self._consolidate_tasks else 0
            )
            pending_notify = list(self._notify_tasks)
            _, still_sending = await asyncio.wait(
                pending_notify,
                timeout=_budget_left(_notify_drain_max_seconds(), reserve),
            )
            # 与下面 consolidation 同一套处理：显式取消 + 给收尾一小段时间，而不是把没跑
            # 完的通知留给 teardown。丢卡是可接受的降级，漏下 CLI 子进程不是。
            for t in still_sending:
                t.cancel()
            if still_sending:
                await asyncio.wait(
                    still_sending,
                    timeout=_budget_left(
                        _notify_kill_seconds(),
                        _consolidate_drain_seconds() + _consolidate_kill_seconds()
                        if self._consolidate_tasks else 0,
                    ),
                )

        # History-triggered consolidations write layer documents, so cancelling
        # one mid-flight is worse than waiting: os.replace makes the write itself
        # atomic, but the history counter is only reset after both layers pass
        # the gates, so an interrupted run re-consolidates next time rather than
        # losing the window. Bounded by one layer's LLM timeout — we are not
        # obliged to finish, only to not corrupt.
        if self._consolidate_tasks:
            pending = list(self._consolidate_tasks)
            _, still_running = await asyncio.wait(
                pending,
                timeout=_budget_left(
                    _consolidate_drain_seconds(), _consolidate_kill_seconds()
                ),
            )
            # asyncio.wait's timeout leaves the unfinished ones pending, and loop
            # teardown would then drop them without their `except CancelledError`
            # ever running — which is where _llm_call kills its helper child.
            # run.sh signals only the backend PID, so an unsignalled glm/cco
            # would be orphaned and keep running (and billing) for up to its own
            # 600s timeout. Cancel explicitly and give the kill paths a moment.
            for t in still_running:
                t.cancel()
            if still_running:
                await asyncio.wait(
                    still_running, timeout=_budget_left(_consolidate_kill_seconds())
                )


# ── helpers ─────────────────────────────────────────────────────────────

_TERMINAL_STATUSES = ("success", "failed")


def _window_tasks(tasks: list, window: int) -> list:
    """The *window* newest finished tasks, for a threshold-triggered pass.

    Two filters, both fixing the same class of bug as the history slice:

    Terminal only. ``_eid_tasks`` returns every task the eid owns, including one
    still running in another member — and ``extract_project_signals`` checks
    neither status nor ``streaming``, so a half-generated agent message that
    happens to contain a path or a command keyword would be persisted as project
    knowledge before that run reached its actual conclusion.

    Newest *window* only. Slicing ``history.md`` alone was not enough: the tasks
    themselves were still every task ever stored, and both extractors walk their
    complete message lists. Old tool errors and corrections that fit under the
    signal cap were therefore re-fed every ten completions, letting the model
    update the same entries again and inflate ``n`` — the exact retention-ranking
    corruption the history slice existed to remove.

    The nightly loop and the 🧠 button do not come through here: they select
    their own task sets (a date, or the last N completed) and are meant to look
    across the whole record.
    """
    from server.auto_memory import status_value

    finished = [t for t in tasks if status_value(t) in _TERMINAL_STATUSES]
    finished.sort(key=lambda t: getattr(t, "updated_at", "") or "", reverse=True)
    return finished[:window] if window > 0 else []


def _clean_env(task_id: str = "") -> dict[str, str]:
    """Return a copy of os.environ with virtualenv and Claude Code session
    markers stripped, optionally tagged with the current agent task id.

    Only the nested-session marker keys (CLAUDECODE / CLAUDE_CODE_ENTRYPOINT /
    CLAUDE_CODE_SSE_PORT) are stripped — other CLAUDE_CODE_* vars
    (``_USE_BEDROCK``, ``_USE_MANTLE``, ``_MAX_OUTPUT_TOKENS`` …) carry
    legitimate backend auth / routing config and must be preserved, otherwise
    Bedrock/Vertex-AI setups lose their routing and fall back to public API
    (or fail entirely).

    ``EPT_CLAUDE_RUNNING`` is the ``ept claude`` wrapper's own re-entrancy
    guard. When agent-park itself runs under ``ept claude``, the server
    inherits it, and every ``ccs`` child would then see it too — the wrapper
    treats that as a nested launch, prints its usage text to stderr and exits
    1 before ever reaching the claude binary. Stripping it lets child agents
    launch normally.

    When ``task_id`` is non-empty we inject ``AGENTPARK_TASK_ID``. Skills
    running inside the agent process (e.g. the agentloop skill) read this to
    self-identify when calling back into agent-park's REST API. The variable
    is intentionally only set for child agent processes — keeping it absent
    from the server's own environment avoids polluting unrelated subshells
    spawned from the main FastAPI process.
    """
    env = os.environ.copy()
    venv = env.pop("VIRTUAL_ENV", None)
    env.pop("VIRTUAL_ENV_PROMPT", None)
    for marker in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
                   "EPT_CLAUDE_RUNNING"):
        env.pop(marker, None)
    if venv:
        venv_bin = os.path.join(venv, "bin")
        path_parts = env.get("PATH", "").split(os.pathsep)
        path_parts = [p for p in path_parts if p != venv_bin]
        env["PATH"] = os.pathsep.join(path_parts)
    if task_id:
        env["AGENTPARK_TASK_ID"] = task_id
    return env


def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from a string."""
    import re
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b\[[^\x40-\x7e]*[\x40-\x7e]", "", s)


runner = AgentRunner()
