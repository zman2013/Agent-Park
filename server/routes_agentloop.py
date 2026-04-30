"""REST routes for agentloop management."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server import agentloop_manager

router = APIRouter(prefix="/api/agentloops")


class StartBody(BaseModel):
    cwd: str
    design_path: str | None = None
    source_task_id: str | None = None
    workspace: str | None = None


@router.get("")
async def list_all(include_dismissed: bool = False):
    return agentloop_manager.list_all(include_dismissed=include_dismissed)


@router.get("/recent")
async def list_recent(limit: int = 5, days: int = 7):
    return agentloop_manager.list_recent(limit=limit, days=days)


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
