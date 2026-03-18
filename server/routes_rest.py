"""REST API routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from server.state import app_state

router = APIRouter(prefix="/api")


class CreateTaskBody(BaseModel):
    prompt: str


@router.get("/agents")
async def list_agents():
    return [a.model_dump() for a in app_state.agents.values()]


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = app_state.get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return task.model_dump()


@router.post("/agents/{agent_id}/tasks")
async def create_task(agent_id: str, body: CreateTaskBody):
    if agent_id not in app_state.agents:
        raise HTTPException(404, "agent not found")
    task = app_state.create_task(agent_id, body.prompt)
    return task.model_dump()


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    from server.agent_runner import runner

    await runner.kill_task(task_id)
    if not app_state.delete_task(task_id):
        raise HTTPException(404, "task not found")
    return {"ok": True}


class UpdateAgentBody(BaseModel):
    cwd: str | None = None


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, body: UpdateAgentBody):
    agent = app_state.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    if body.cwd is not None:
        agent.cwd = body.cwd
    from server.routes_ws import broadcast
    await broadcast({"type": "state_sync", "data": app_state.snapshot()})
    return agent.model_dump()
