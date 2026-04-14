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
_heartbeat_task: asyncio.Task | None = None
_daily_summary_task: asyncio.Task | None = None
_wiki_ingest_task: asyncio.Task | None = None


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
    """Sleep until next midnight (local time), then run summary for all agents."""
    from datetime import datetime, timedelta

    while True:
        now = datetime.now()
        # Next midnight
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        sleep_seconds = (next_midnight - now).total_seconds()
        logger.info(
            "Daily summary scheduled in %.0f s (at %s)",
            sleep_seconds,
            next_midnight.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await asyncio.sleep(sleep_seconds)

        # Run summary for every agent
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        logger.info("Running daily knowledge summary for date %s", yesterday)
        for agent_id in list(app_state.agents.keys()):
            try:
                await _run_daily_summary(agent_id, yesterday)
            except Exception:
                logger.exception("Daily summary failed for agent %s", agent_id)


async def _run_daily_summary(agent_id: str, date: str) -> None:
    """Run knowledge summary for a single agent for a specific date."""
    from server.knowledge import generate_summary

    agent = app_state.get_agent(agent_id)
    tasks = [
        app_state.tasks[tid]
        for tid in (agent.task_ids if agent else [])
        if tid in app_state.tasks
    ]
    # Filter to tasks updated on the target date
    tasks = [t for t in tasks if (getattr(t, "updated_at", "") or "").startswith(date)]
    if not tasks:
        logger.info("No tasks for agent %s on %s, skipping summary", agent_id, date)
        return

    logger.info(
        "Daily summary: agent=%s date=%s tasks=%d", agent_id, date, len(tasks)
    )
    result = await generate_summary(agent_id, tasks)
    logger.info(
        "Daily summary done: agent=%s files=%s memory_entries=%d",
        agent_id,
        result.get("files_updated"),
        result.get("memory_entries", 0),
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
    from server.wiki_ingest import ingest_agent_tasks
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
        except Exception:
            logger.exception("Wiki ingest failed for agent %s", agent_id)
            all_results.append({
                "agent_id": agent_id,
                "wiki": wiki_name,
                "error": str(Exception),
            })

    if all_results:
        # Send feishu digest notification
        try:
            await send_wiki_digest(feishu_cfg, all_results, date)
        except Exception:
            logger.exception("Wiki digest notification failed")


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
    await ws.send_text(
        json.dumps({"type": "state_sync", "data": app_state.snapshot(_runner._session_ids)}, ensure_ascii=False)
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
        from server.agent_runner import runner

        await runner.send_input(task_id, content)

    elif msg_type == "fork_task":
        source_task_id = data.get("task_id", "")
        source_task = app_state.get_task(source_task_id)
        if not source_task:
            return
        from server.agent_runner import runner as _runner
        source_session_id = _runner._session_ids.get(source_task_id)
        if not source_session_id:
            return
        try:
            new_task = app_state.fork_task(source_task_id, source_session_id)
        except ValueError:
            return
        await broadcast(task_created_message(new_task))

    elif msg_type == "generate_summary":
        from server.config import knowledge_config
        if not knowledge_config().get("enabled", True):
            logger.info("generate_summary ignored: knowledge summary is disabled by config")
            return
        agent_id = data.get("agent_id", "")
        date_range = data.get("date_range", "recent_n")
        if not agent_id or agent_id not in app_state.agents:
            return
        asyncio.create_task(_run_generate_summary(agent_id, date_range))


async def _run_generate_summary(agent_id: str, date_range: str) -> None:
    """Run knowledge summary generation and broadcast progress."""
    from server.knowledge import generate_summary
    from server.config import knowledge_config

    async def progress_cb(step: str, detail: str):
        await broadcast({
            "type": "summary_progress",
            "agent_id": agent_id,
            "step": step,
            "detail": detail,
        })

    try:
        cfg = knowledge_config()
        agent = app_state.get_agent(agent_id)
        tasks = [app_state.tasks[tid] for tid in (agent.task_ids if agent else []) if tid in app_state.tasks]
        if date_range == "today":
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            tasks = [t for t in tasks if (t.updated_at or "").startswith(today)]
        else:
            # recent_n: last N completed tasks
            n = cfg.get("default_task_count", 5)
            completed = [t for t in tasks if str(t.status) in ("success", "failed")]
            completed.sort(key=lambda t: t.updated_at or "", reverse=True)
            tasks = completed[:n]

        result = await generate_summary(agent_id, tasks, progress_cb)
        await broadcast({
            "type": "summary_done",
            "agent_id": agent_id,
            "files_updated": result.get("files_updated", []),
            "memory_entries": result.get("memory_entries", 0),
        })
    except Exception as exc:
        logger.exception("generate_summary failed for agent %s", agent_id)
        await broadcast({
            "type": "summary_error",
            "agent_id": agent_id,
            "error": str(exc),
        })
