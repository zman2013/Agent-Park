"""REST API routes."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from server.state import app_state

router = APIRouter(prefix="/api")


class CreateTaskBody(BaseModel):
    prompt: str


@router.get("/agents")
async def list_agents():
    return [a.model_dump() for a in app_state.agents.values()]


class CreateAgentBody(BaseModel):
    name: str
    command: str = "cco"
    cwd: str = ""


@router.post("/agents")
async def create_agent(body: CreateAgentBody):
    try:
        agent = app_state.create_agent(body.name, body.command, body.cwd)
    except ValueError as e:
        raise HTTPException(409, str(e))
    from server.routes_ws import broadcast
    await broadcast({"type": "state_sync", "data": app_state.snapshot()})
    return agent.model_dump()


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
    name: str | None = None
    cwd: str | None = None


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, body: UpdateAgentBody):
    agent = app_state.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    if body.name is not None:
        agent.name = body.name
    if body.cwd is not None:
        agent.cwd = body.cwd
    from server.routes_ws import broadcast
    await broadcast({"type": "state_sync", "data": app_state.snapshot()})
    return agent.model_dump()


class ReorderAgentsBody(BaseModel):
    order: list[str]


@router.post("/agents/reorder")
async def reorder_agents(body: ReorderAgentsBody):
    app_state.reorder_agents(body.order)
    from server.routes_ws import broadcast
    await broadcast({"type": "state_sync", "data": app_state.snapshot()})
    return {"ok": True}


class UpdateTaskBody(BaseModel):
    name: str | None = None


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, body: UpdateTaskBody):
    task = app_state.get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    if body.name is not None:
        task.name = body.name
    app_state.save_tasks()
    from server.routes_ws import broadcast
    await broadcast({"type": "state_sync", "data": app_state.snapshot()})
    return task.model_dump()


class ShellExecBody(BaseModel):
    cwd: str = ""
    command: str


# ── Memory endpoints ──────────────────────────────────────────────────────────

class MemoryAddBody(BaseModel):
    content: str
    type: str = "note"


@router.get("/agents/{agent_id}/memory")
async def get_memory(agent_id: str):
    if agent_id not in app_state.agents:
        raise HTTPException(404, "agent not found")
    from server.memory import list_memory
    return list_memory(agent_id)


@router.post("/agents/{agent_id}/memory")
async def add_memory(agent_id: str, body: MemoryAddBody):
    if agent_id not in app_state.agents:
        raise HTTPException(404, "agent not found")
    from server.memory import compress_content, append_memory, MAX_CONTENT_LENGTH, _utcnow_iso
    from server.config import memory_config

    command = memory_config()["command"]
    compressed = await compress_content(body.content, command)

    if len(compressed) > MAX_CONTENT_LENGTH:
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"压缩后内容过长（{len(compressed)} 字符），超过限制 {MAX_CONTENT_LENGTH} 字符，请精简输入内容",
                "compressed": compressed,
            },
        )

    entry = {"type": body.type, "timestamp": _utcnow_iso(), "content": compressed}
    append_memory(agent_id, entry)
    return entry


@router.delete("/agents/{agent_id}/memory/{line_index}")
async def delete_memory(agent_id: str, line_index: int):
    if agent_id not in app_state.agents:
        raise HTTPException(404, "agent not found")
    from server.memory import delete_memory_line
    if not delete_memory_line(agent_id, line_index):
        raise HTTPException(404, "memory entry not found")
    return {"ok": True}


@router.get("/ept-usage")
async def ept_usage():
    """Run `ept usage` and return the monthly total cost."""
    import re
    try:
        proc = await asyncio.create_subprocess_exec(
            "ept", "usage",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ},
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        text = stdout.decode("utf-8", errors="replace")
        # Extract: 本月使用总金额: 5,969.13 元
        m = re.search(r"本月使用总金额[^:：]*[:：]\s*([\d,]+(?:\.\d+)?)\s*元", text)
        if m:
            amount = m.group(1).replace(",", "")
            return {"amount": float(amount), "currency": "元"}
        return {"amount": None, "currency": "元", "raw": text[:500]}
    except Exception as e:
        return {"amount": None, "currency": "元", "error": str(e)}


@router.post("/shell/exec")
async def shell_exec(body: ShellExecBody):
    cwd = body.cwd or None

    async def stream_output():
        try:
            proc = await asyncio.create_subprocess_shell(
                body.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                env={**os.environ},
            )
            async for line in proc.stdout:
                yield line
            await proc.wait()
        except FileNotFoundError as e:
            yield f"Error: {e}\n".encode()
        except Exception as e:
            yield f"Error: {e}\n".encode()

    return StreamingResponse(
        stream_output(),
        media_type="text/plain; charset=utf-8",
        headers={"X-Accel-Buffering": "no"},
    )
