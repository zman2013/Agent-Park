"""WebSocket endpoint – single ws://host/ws connection per client."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.state import app_state

router = APIRouter()
logger = logging.getLogger(__name__)

# Connected clients
clients: set[WebSocket] = set()
SEND_TIMEOUT_SECONDS = 2.0
HEARTBEAT_INTERVAL_SECONDS = 20.0
AGENTLOOP_NOTIFY_INTERVAL_SECONDS = 60.0
# Staggered off 00:00 so knowledge consolidation and wiki ingest don't both
# wake into a serial pile of LLM calls at midnight.
DAILY_SUMMARY_HOUR = 0
DAILY_SUMMARY_MINUTE = 30
_heartbeat_task: asyncio.Task | None = None
_daily_summary_task: asyncio.Task | None = None
_wiki_ingest_task: asyncio.Task | None = None
_agentloop_notify_task: asyncio.Task | None = None


async def broadcast(msg: dict[str, Any]) -> None:
    payload = json.dumps(msg, ensure_ascii=False)
    targets = list(clients)
    if not targets:
        return

    async def _send(ws: WebSocket) -> Exception | None:
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=SEND_TIMEOUT_SECONDS)
            return None
        except Exception as exc:
            return exc

    results = await asyncio.gather(*(_send(ws) for ws in targets), return_exceptions=False)
    dead: list[WebSocket] = []
    for ws, result in zip(targets, results):
        if result is None:
            continue
        dead.append(ws)
        logger.warning("Dropping WS client during broadcast: %r", result)

    for ws in dead:
        clients.discard(ws)


def _ensure_heartbeat_task() -> None:
    global _heartbeat_task
    if _heartbeat_task is None or _heartbeat_task.done():
        _heartbeat_task = asyncio.create_task(_heartbeat_loop(), name="ws-heartbeat")




async def _heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        await broadcast({"type": "ping"})


def ensure_daily_summary_task() -> None:
    """Start the daily summary background loop if not already running."""
    global _daily_summary_task
    if _daily_summary_task is None or _daily_summary_task.done():
        _daily_summary_task = asyncio.create_task(
            _daily_summary_loop(), name="daily-summary"
        )


async def _daily_summary_loop() -> None:
    """Sleep until the daily slot, then consolidate knowledge per effective id."""
    from datetime import datetime, timedelta

    while True:
        now = datetime.now()
        target = now.replace(
            hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE,
            second=0, microsecond=0,
        )
        if target <= now:
            target = target + timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        logger.info(
            "Daily summary scheduled in %.0f s (at %s)",
            sleep_seconds,
            target.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await asyncio.sleep(sleep_seconds)

        # Consolidate the day that just ended, not "now".
        target_date = (target - timedelta(days=1)).strftime("%Y-%m-%d")
        await run_daily_summary_all(target_date)


async def run_daily_summary_all(date: str) -> None:
    """Consolidate every active effective id for *date*.

    Iterates effective ids rather than agent ids: several agents share one
    memory store, so running once per agent would re-run the LLM N times over
    the same documents (one eid here has 478 members).
    """
    from server.auto_memory import active_eids

    logger.info("Running daily consolidation for date %s", date)
    for eid in active_eids():
        try:
            await _run_daily_summary(eid, date)
        except Exception:
            logger.exception("Daily summary failed for eid %s", eid)


def _eid_tasks(eid: str) -> list:
    """Return every task belonging to any agent that maps to *eid*."""
    from server.auto_memory import eid_members

    tasks = []
    seen: set[str] = set()
    for aid in eid_members(eid):
        agent = app_state.get_agent(aid)
        for tid in agent.task_ids if agent else []:
            if tid in seen:
                continue
            task = app_state.tasks.get(tid)
            if task is not None:
                seen.add(tid)
                tasks.append(task)
    return tasks


async def _run_daily_summary(eid: str, date: str) -> None:
    """Consolidate one effective id for a specific date."""
    from server.auto_memory import consolidate

    all_tasks = _eid_tasks(eid)
    day_tasks = [t for t in all_tasks if (getattr(t, "updated_at", "") or "").startswith(date)]
    if not day_tasks:
        logger.info("No tasks for eid %s on %s, skipping summary", eid, date)
        return

    logger.info(
        "Daily summary: eid=%s date=%s tasks=%d members_tasks=%d",
        eid, date, len(day_tasks), len(all_tasks),
    )
    # today=date, not the wall clock. This path deliberately consolidates the
    # day that just ended, and `last` feeds the truncation ranking: stamping
    # "now" dates every nightly result one day late, and dates a replay of old
    # history as the replay day — which is how `last` stops discriminating and
    # the recency half of the ranking goes quietly dead.
    result = await consolidate(eid, day_tasks, today=date)
    logger.info(
        "Daily summary done: eid=%s added=%d updated=%d deleted=%d refused=%d%s",
        eid, result["added"], result["updated"], result["deleted"], result["refused"],
        f" FAILED_LAYERS={result['failed_layers']}" if result["failed_layers"] else "",
    )


# ── Daily Wiki Ingest ───────────────────────────────────────────────────────────────


def ensure_wiki_ingest_task() -> None:
    """Start the daily wiki ingest background loop if not already running."""
    global _wiki_ingest_task
    if _wiki_ingest_task is None or _wiki_ingest_task.done():
        _wiki_ingest_task = asyncio.create_task(
            _wiki_ingest_loop(), name="wiki-ingest"
        )


async def _wiki_ingest_loop() -> None:
    """Sleep until configured time (default midnight local), then run wiki ingest for all agents."""
    from datetime import datetime, timedelta
    from server.config import wiki_ingest_config

    while True:
        cfg = wiki_ingest_config()
        schedule = cfg.get("schedule", {})
        if not schedule.get("enabled", True):
            # Sleep 1 hour then re-check in case config changed
            await asyncio.sleep(3600)
            continue

        target_hour = schedule.get("hour", 0)
        target_minute = schedule.get("minute", 0)

        now = datetime.now()
        target = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        if target <= now:
            # Already passed today, schedule for tomorrow
            target = target + timedelta(days=1)

        sleep_seconds = (target - now).total_seconds()
        logger.info(
            "Wiki ingest scheduled in %.0f s (at %s)",
            sleep_seconds,
            target.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await asyncio.sleep(sleep_seconds)

        # Ingest the completed day, not "now". For the default midnight run,
        # this should process yesterday's tasks.
        target_date = (target - timedelta(days=1)).strftime("%Y-%m-%d")
        logger.info(
            "Running daily wiki ingest for date %s (scheduled_at=%s)",
            target_date,
            target.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await _run_daily_wiki_ingest(target_date)


async def _run_daily_wiki_ingest(date: str) -> None:
    """Run wiki ingest for all agents that have a wiki configured."""
    from server.wiki_ingest import ingest_agent_tasks, maybe_trigger_memforge_reindex
    from server.wiki_notify import send_wiki_digest
    from server.config import wiki_ingest_config

    cfg = wiki_ingest_config()
    feishu_cfg = cfg.get("feishu_notify", {})

    all_results: list[dict] = []

    for agent_id in list(app_state.agents.keys()):
        agent = app_state.get_agent(agent_id)
        if not agent:
            continue
        wiki_name = getattr(agent, "wiki", None)
        if not wiki_name:
            logger.info("Agent %s has no wiki configured, skipping ingest", agent_id)
            continue

        logger.info("Wiki ingest: agent=%s wiki=%s date=%s", agent_id, wiki_name, date)
        try:
            result = await ingest_agent_tasks(agent_id, target_date=date)
            all_results.append(result)
            logger.info(
                "Wiki ingest done: agent=%s wiki=%s processed=%d skipped=%d",
                agent_id,
                wiki_name,
                result.get("tasks_processed", 0),
                result.get("tasks_skipped", 0),
            )
        except Exception as exc:
            logger.exception("Wiki ingest failed for agent %s", agent_id)
            all_results.append({
                "agent_id": agent_id,
                "wiki": wiki_name,
                "error": str(exc),
            })

    if all_results:
        # Send feishu digest notification
        try:
            await send_wiki_digest(feishu_cfg, all_results, date)
        except Exception:
            logger.exception("Wiki digest notification failed")

    # Refresh memforge vector index so the next agent prompts see new knowledge.
    # No-op unless wiki_ingest.memforge_reindex_enabled=true; failures are
    # logged and do not affect the scheduler loop.
    try:
        await maybe_trigger_memforge_reindex()
    except Exception:
        logger.exception("memforge reindex hook crashed")


def task_created_message(task) -> dict[str, Any]:
    agent = app_state.get_agent(task.agent_id)
    return {
        "type": "task_created",
        "agent_id": task.agent_id,
        "task": task.model_dump(),
        "task_ids": list(agent.task_ids) if agent else [task.id],
    }


def agents_reordered_message(order: list[str], request_id: int | None = None) -> dict[str, Any]:
    return {
        "type": "agents_reordered",
        "order": order,
        "request_id": request_id,
    }


def task_updated_message(task) -> dict[str, Any]:
    return {
        "type": "task_updated",
        "task_id": task.id,
        "fields": {"name": task.name, "status": task.status, "updated_at": task.updated_at},
    }


def agent_updated_message(agent, fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "agent_updated",
        "agent_id": agent.id,
        "fields": fields,
    }


def agent_created_message(agent, order: list[str]) -> dict[str, Any]:
    return {
        "type": "agent_created",
        "agent": agent.model_dump(),
        "order": order,
    }


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    # Send initial state before adding to broadcast list, so concurrent
    # broadcasts don't race against a connection that isn't ready yet.
    from server.agent_runner import runner as _runner
    sync_data = app_state.snapshot(_runner._session_ids)
    sync_data["auto_compact_disabled"] = list(_runner._auto_compact_disabled)
    sync_data["plan_mode"] = list(_runner._plan_mode)
    await ws.send_text(
        json.dumps({"type": "state_sync", "data": sync_data}, ensure_ascii=False)
    )

    clients.add(ws)
    _ensure_heartbeat_task()
    logger.info("WS client connected (%d total)", len(clients))

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            await _handle_client_message(data, ws)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        logger.info("WS client disconnected (%d remaining)", len(clients))


async def _handle_client_message(data: dict, ws: WebSocket) -> None:
    msg_type = data.get("type")

    if msg_type == "create_task":
        agent_id = data["agent_id"]
        name = data.get("name", "")
        if agent_id not in app_state.agents:
            return
        task = app_state.create_task(agent_id, name)
        await broadcast(task_created_message(task))

    elif msg_type == "user_message":
        task_id = data["task_id"]
        content = data.get("content", "")
        task = app_state.get_task(task_id)
        if not task:
            return

        from server.models import Message
        from server.agent_runner import runner

        # Same per-task lock the feishu inbound path takes, so the two input
        # sources serialize instead of each starting a run and killing the
        # other's subprocess. A browser message is an explicit user action, so
        # it queues behind the holder rather than being rejected.
        async with runner.input_lock(task_id):
            user_msg = Message(role="user", content=content)
            task.messages.append(user_msg)
            app_state.save_agent_tasks(task.agent_id)
            await broadcast(
                {
                    "type": "message",
                    "task_id": task_id,
                    "message": user_msg.model_dump(),
                }
            )

            # Send input to agent process
            await runner.send_input(task_id, content)

    elif msg_type == "trigger_compact":
        task_id = data.get("task_id", "")
        task = app_state.get_task(task_id)
        if not task:
            return
        from server.models import Message, TaskStatus
        from server.agent_runner import runner

        # Same per-task input lock as user_message / feishu inbound: the guard
        # below and send_input() must be one atomic step, else a concurrent
        # input passes its own guard and kills whichever run started first.
        async with runner.input_lock(task_id):
            # Reject /compact while the agent is running. Without this guard a
            # stale tab, a duplicated click before the status update lands, or a
            # crafted WS message could send /compact while the agent is still
            # running, killing the in-flight turn and replacing it with /compact.
            # Idle statuses (waiting/success/failed) are all acceptable since the
            # frontend button only disables on `running`.
            if task.status == TaskStatus.running:
                return

            # Notify the user via a system bubble; '/compact' itself is not
            # stored as a user message so the chat stays clean.
            notice = Message(
                role="agent",
                type="system",
                streaming=False,
                content="🤖 已发送 /compact 指令，正在压缩上下文…",
            )
            task.messages.append(notice)
            app_state.save_agent_tasks(task.agent_id)
            await broadcast({
                "type": "message",
                "task_id": task_id,
                "message": notice.model_dump(),
            })
            runner._compact_pending.discard(task_id)
            await runner.send_input(task_id, "/compact")

    elif msg_type == "toggle_auto_compact":
        task_id = data.get("task_id", "")
        disabled = bool(data.get("disabled", False))
        task = app_state.get_task(task_id)
        if not task:
            return
        from server.agent_runner import runner
        runner.toggle_auto_compact(task_id, disabled)
        await broadcast({
            "type": "auto_compact_toggled",
            "task_id": task_id,
            "disabled": disabled,
        })

    elif msg_type == "toggle_plan_mode":
        task_id = data.get("task_id", "")
        enabled = bool(data.get("enabled", False))
        task = app_state.get_task(task_id)
        if not task:
            return
        from server.agent_runner import runner
        runner.toggle_plan_mode(task_id, enabled)
        await broadcast({
            "type": "plan_mode_toggled",
            "task_id": task_id,
            "enabled": enabled,
        })

    elif msg_type == "trigger_handoff":
        task_id = data.get("task_id", "")
        task = app_state.get_task(task_id)
        if not task:
            return
        from server.models import Message, TaskStatus
        from server.agent_runner import runner
        from server.handoff_prompts import step_1_and_2

        # Same per-task input lock as the other input paths (see trigger_compact).
        async with runner.input_lock(task_id):
            # Reject while running (mirrors /compact). Allow idle/waiting/success/failed.
            if task.status == TaskStatus.running:
                return

            notice = Message(
                role="agent",
                type="system",
                streaming=False,
                content="📤 已发起 Handoff 流程：整理 docs/handoff/ 并同步 sync-principles…",
            )
            task.messages.append(notice)
            app_state.save_agent_tasks(task.agent_id)
            await broadcast({
                "type": "message",
                "task_id": task_id,
                "message": notice.model_dump(),
            })

            runner._handoff_pending.add(task_id)
            await runner.send_input(task_id, step_1_and_2())

    elif msg_type == "stop_task":
        task_id = data.get("task_id", "")
        task = app_state.get_task(task_id)
        if not task:
            return
        from server.models import Message, TaskStatus
        from server.agent_runner import runner

        if task.status != TaskStatus.running:
            return

        await runner.kill_task(task_id)

        # Clear any pending auto-compact flag set during this turn. Without
        # this, a follow-up successful turn in the same task would inherit
        # the stale flag and unexpectedly auto-send /compact.
        await runner.maybe_dispatch_auto_compact(task_id, success=False)

        # Same for handoff: a stopped turn would otherwise leave _handoff_pending
        # dangling, so the next unrelated successful turn on this task would
        # unexpectedly auto-send the handoff prompt again.
        await runner.maybe_dispatch_handoff(task_id, success=False)

        # Finalize any in-flight streaming bubbles. kill_task() cancels the
        # subprocess before the adapter can emit content_block_stop /
        # message_done, so without this the stopped transcript would keep
        # rendering old bubbles as streaming after reload.
        for msg in task.messages:
            if msg.streaming:
                msg.streaming = False
                await broadcast({
                    "type": "message_done",
                    "task_id": task_id,
                    "message_id": msg.id,
                })

        notice = Message(
            role="agent",
            type="system",
            streaming=False,
            content="🛑 已停止任务，session 已中断。",
        )
        task.messages.append(notice)
        await broadcast({
            "type": "message",
            "task_id": task_id,
            "message": notice.model_dump(),
        })
        await runner._finish_task(task_id, TaskStatus.failed)

    elif msg_type == "fork_task":
        source_task_id = data.get("task_id", "")
        source_task = app_state.get_task(source_task_id)
        if not source_task:
            return
        from server.agent_runner import runner as _runner
        source_session_id = _runner._session_ids.get(source_task_id)
        if not source_session_id:
            return
        # Resolve at_message_id (agent-park Message.id) to CCo native uuid
        resume_at = None
        at_message_id = data.get("at_message_id", "")
        if at_message_id:
            msg = next((m for m in source_task.messages if m.id == at_message_id), None)
            if not msg or not msg.cco_uuid:
                await broadcast({"type": "error", "message": "该消息没有 cco_uuid，无法从此处 Fork（消息可能来自旧 task）"})
                return
            resume_at = msg.cco_uuid
        try:
            new_task = app_state.fork_task(source_task_id, source_session_id, resume_at=resume_at)
        except ValueError:
            return
        await broadcast(task_created_message(new_task))

    elif msg_type == "generate_summary":
        agent_id = data.get("agent_id", "")
        date_range = data.get("date_range", "recent_n")
        if not agent_id or agent_id not in app_state.agents:
            return
        asyncio.create_task(_run_generate_summary(agent_id, date_range))


async def _run_generate_summary(agent_id: str, date_range: str) -> None:
    """Consolidate on demand (the 🧠 button) and broadcast progress.

    Runs regardless of ``automemory.daily_enabled``: that flag only silences the
    unattended loop, and the manual path is how consolidation gets exercised
    against real history.
    """
    from server.auto_memory import consolidate, effective_id
    from server.config import automemory_config

    async def progress_cb(step: str, detail: str):
        await broadcast({
            "type": "summary_progress",
            "agent_id": agent_id,
            "step": step,
            "detail": detail,
        })

    try:
        cfg = automemory_config()
        eid = effective_id(agent_id)
        # Aggregate across every agent sharing this store, matching the daily
        # loop: the documents are shared, so a manual run must see the same
        # task set the unattended one would.
        all_tasks = _eid_tasks(eid)
        tasks = all_tasks
        if date_range == "today":
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            tasks = [t for t in tasks if (t.updated_at or "").startswith(today)]
        else:
            # recent_n: last N completed tasks. status_value, not str(): status is
            # a str-Enum whose str() is "TaskStatus.success", so this filter used
            # to match nothing and the button consolidated an empty task list.
            n = cfg.get("default_task_count", 5)
            from server.auto_memory import status_value
            completed = [t for t in tasks if status_value(t) in ("success", "failed")]
            completed.sort(key=lambda t: t.updated_at or "", reverse=True)
            tasks = completed[:n]

        result = await consolidate(eid, tasks, progress_cb=progress_cb)
        await broadcast({
            "type": "summary_done",
            "agent_id": agent_id,
            # The four counts are the whole observability story: they are what
            # distinguishes "nothing needed changing" from "the model returned
            # something we refused to write".
            "added": result["added"],
            "updated": result["updated"],
            "deleted": result["deleted"],
            "refused": result["refused"],
            "failed_layers": result["failed_layers"],
        })
    except Exception as exc:
        logger.exception("generate_summary failed for agent %s", agent_id)
        await broadcast({
            "type": "summary_error",
            "agent_id": agent_id,
            "error": str(exc),
        })


# ── AgentLoop Completion Notify ────────────────────────────────────────────


def ensure_agentloop_notify_task() -> None:
    """Start the periodic agentloop completion-notify loop if not running.

    The route handlers (``GET /api/agentloops*``) also schedule notifications
    on demand, but only when something queries them. This background poller
    is the fallback path: even if the user never opens the UI, finished loops
    still get reported back to their source agent task within a minute.
    """
    global _agentloop_notify_task
    if _agentloop_notify_task is None or _agentloop_notify_task.done():
        _agentloop_notify_task = asyncio.create_task(
            _agentloop_notify_loop(), name="agentloop-notify"
        )


async def _agentloop_notify_loop() -> None:
    """Sweep the agentloop registry every 60 s and notify any newly finished
    loops back to their source agent task. Idempotency lives in
    ``agentloop_manager.notify_source_task`` (``notified_at`` flag), so this
    loop is safe to run alongside the route-handler triggered path.
    """
    from server import agentloop_manager

    while True:
        try:
            await asyncio.sleep(AGENTLOOP_NOTIFY_INTERVAL_SECONDS)
            delivered = await agentloop_manager.notify_pending()
            if delivered:
                logger.info("AgentLoop notify: delivered %d completion message(s)", delivered)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Never let a single iteration error kill the long-lived task —
            # log and keep looping. The 60 s sleep above already provides
            # natural backoff.
            logger.exception("AgentLoop notify loop iteration failed")
