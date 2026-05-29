"""REST routes for agentloop management."""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server import agentloop_manager

router = APIRouter(prefix="/api/agentloops")
logger = logging.getLogger(__name__)


class StartBody(BaseModel):
    cwd: str
    design_path: str | None = None
    source_task_id: str | None = None
    workspace: str | None = None


def _schedule_pending_notifications(entries: Iterable[dict]) -> None:
    """Fire-and-forget: hand each finalized-but-unnotified loop to the notifier.

    Runs as a background asyncio task so the GET response isn't blocked on
    file I/O + broadcast. Idempotency is enforced inside ``notify_source_task``
    via ``notified_at``, so even if the background poller and a concurrent
    GET both schedule the same loop, only one notification is delivered.
    """
    loop_ids = [
        e["loop_id"] for e in entries
        if not e.get("notified_at")
        and (e.get("status") or "").lower() in agentloop_manager._NOTIFIABLE_STATUSES
        and e.get("loop_id")
    ]
    if not loop_ids:
        return

    async def _runner() -> None:
        for lid in loop_ids:
            try:
                await agentloop_manager.notify_source_task(lid)
            except Exception:  # noqa: BLE001
                logger.exception("agentloop notify failed: loop_id=%s", lid)

    asyncio.create_task(_runner(), name="agentloop-notify-on-refresh")


@router.get("")
async def list_all(include_dismissed: bool = False):
    entries = agentloop_manager.list_all(include_dismissed=include_dismissed)
    _schedule_pending_notifications(entries)
    return entries


@router.get("/recent")
async def list_recent(limit: int = 5, days: int = 7):
    entries = agentloop_manager.list_recent(limit=limit, days=days)
    _schedule_pending_notifications(entries)
    return entries


@router.post("")
async def start_loop(body: StartBody):
    try:
        entry = agentloop_manager.start(
            cwd=body.cwd,
            design_path=body.design_path,
            source_task_id=body.source_task_id,
            workspace=body.workspace,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"failed to start agentloop: {e}")
    return entry


@router.get("/{loop_id}")
async def get_detail(loop_id: str):
    snap = agentloop_manager.get_snapshot(loop_id)
    if not snap:
        raise HTTPException(404, "agentloop not found")
    _schedule_pending_notifications([snap])
    return snap


@router.get("/{loop_id}/runs/{cycle}")
async def get_run(loop_id: str, cycle: int):
    lines = agentloop_manager.get_run_log(loop_id, cycle)
    if lines is None:
        raise HTTPException(404, "run log not found")
    return {"loop_id": loop_id, "cycle": cycle, "lines": lines}


@router.post("/{loop_id}/stop")
async def stop_loop(loop_id: str):
    entry = agentloop_manager.stop(loop_id)
    if entry is None:
        raise HTTPException(404, "agentloop not found")
    return entry


@router.post("/{loop_id}/dismiss")
async def dismiss_loop(loop_id: str):
    entry = agentloop_manager.dismiss(loop_id)
    if entry is None:
        raise HTTPException(404, "agentloop not found")
    return entry
