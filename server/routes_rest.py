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
    shared_memory_agent_id: str | None = None


@router.post("/agents")
async def create_agent(body: CreateAgentBody):
    try:
        agent = app_state.create_agent(body.name, body.command, body.cwd, body.shared_memory_agent_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
    from server.routes_ws import broadcast, agent_created_message
    await broadcast(agent_created_message(agent, app_state.ordered_agent_ids()))
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
    from server.routes_ws import broadcast, task_created_message
    await broadcast(task_created_message(task))
    return task.model_dump()


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    from server.agent_runner import runner

    task = app_state.get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    agent_id = task.agent_id
    await runner.kill_task(task_id)
    app_state.delete_task(task_id)
    agent = app_state.get_agent(agent_id)
    from server.routes_ws import broadcast
    await broadcast({
        "type": "task_deleted",
        "task_id": task_id,
        "agent_id": agent_id,
        "task_ids": list(agent.task_ids) if agent else [],
    })
    return {"ok": True}


class UpdateAgentBody(BaseModel):
    name: str | None = None
    cwd: str | None = None
    command: str | None = None
    shared_memory_agent_id: str | None = None
    clear_shared_memory: bool = False


@router.patch("/agents/{agent_id}")
async def update_agent(agent_id: str, body: UpdateAgentBody):
    agent = app_state.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    changed: dict = {}
    if body.name is not None:
        agent.name = body.name
        changed["name"] = agent.name
    if body.cwd is not None:
        agent.cwd = body.cwd
        changed["cwd"] = agent.cwd
    if body.command is not None:
        agent.command = body.command
        changed["command"] = agent.command
    if body.shared_memory_agent_id is not None:
        agent.shared_memory_agent_id = body.shared_memory_agent_id
        changed["shared_memory_agent_id"] = agent.shared_memory_agent_id
    if body.clear_shared_memory:
        agent.shared_memory_agent_id = None
        changed["shared_memory_agent_id"] = None
    app_state.save_agents()
    from server.routes_ws import broadcast, agent_updated_message
    await broadcast(agent_updated_message(agent, changed))
    return agent.model_dump()


class ReorderAgentsBody(BaseModel):
    order: list[str]
    request_id: int | None = None


@router.post("/agents/reorder")
async def reorder_agents(body: ReorderAgentsBody):
    app_state.reorder_agents(body.order)
    order = app_state.ordered_agent_ids()
    from server.routes_ws import agents_reordered_message, broadcast
    await broadcast(agents_reordered_message(order, body.request_id))
    return {"ok": True, "order": order, "request_id": body.request_id}


@router.post("/agents/{agent_id}/pin")
async def pin_agent(agent_id: str):
    try:
        app_state.pin_agent(agent_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    agent = app_state.agents[agent_id]
    from server.routes_ws import broadcast, agent_updated_message, agents_reordered_message
    await broadcast(agent_updated_message(agent, {"pinned": True}))
    await broadcast(agents_reordered_message(app_state.ordered_agent_ids()))
    return agent.model_dump()


@router.post("/agents/{agent_id}/unpin")
async def unpin_agent(agent_id: str):
    try:
        app_state.unpin_agent(agent_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    agent = app_state.agents[agent_id]
    from server.routes_ws import broadcast, agent_updated_message, agents_reordered_message
    await broadcast(agent_updated_message(agent, {"pinned": False}))
    await broadcast(agents_reordered_message(app_state.ordered_agent_ids()))
    return agent.model_dump()


class UpdateTaskBody(BaseModel):
    name: str | None = None


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, body: UpdateTaskBody):
    task = app_state.get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    if body.name is not None:
        task.name = body.name
    app_state.save_agent_tasks(task.agent_id)
    from server.routes_ws import broadcast, task_updated_message
    await broadcast(task_updated_message(task))
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


@router.get("/skills")
async def list_skills(cwd: str = ""):
    """Return available skills from ~/.claude/skills/ and project-level .claude/skills/."""
    import re
    from pathlib import Path

    def scan_skills_dir(skills_dir: Path) -> list[dict]:
        if not skills_dir.exists():
            return []
        result = []
        for item in sorted(skills_dir.iterdir()):
            if not (item.is_dir() or item.is_symlink()):
                continue
            skill_md = item / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
            except Exception:
                continue
            m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if not m:
                continue
            fm = m.group(1)
            name_m = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
            desc_m = re.search(r"^description:\s*\|\s*\n((?:  .+\n?)+)", fm, re.MULTILINE)
            if not desc_m:
                desc_m = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
                desc = desc_m.group(1).strip() if desc_m else ""
            else:
                lines = desc_m.group(1).strip().split("\n")
                desc = " ".join(ln.strip() for ln in lines)
            name = name_m.group(1).strip() if name_m else item.name
            result.append({"name": name, "description": desc})
        return result

    global_skills = scan_skills_dir(Path.home() / ".claude" / "skills")

    project_skills: list[dict] = []
    if cwd:
        project_dir = Path(cwd)
        if project_dir.is_dir():
            project_skills = scan_skills_dir(project_dir / ".claude" / "skills")

    # Merge: project skills override global skills with the same name
    global_names = {s["name"] for s in project_skills}
    merged = project_skills + [s for s in global_skills if s["name"] not in global_names]
    merged.sort(key=lambda s: s["name"])

    return merged


# ── File browser endpoints ─────────────────────────────────────────────────────

IGNORED_NAMES = {
    "node_modules", ".git", "__pycache__", ".DS_Store",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _resolve_agent_path(agent_id: str, rel_path: str) -> tuple[str, str]:
    """Return (cwd, abs_path) or raise HTTPException."""
    agent = app_state.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    if not agent.cwd:
        raise HTTPException(400, "agent has no cwd configured")
    cwd = os.path.realpath(agent.cwd)
    if not os.path.isdir(cwd):
        raise HTTPException(400, "agent cwd does not exist")
    # Normalise path: strip leading slashes
    clean = rel_path.lstrip("/").replace("..", "").lstrip("/")
    abs_path = os.path.realpath(os.path.join(cwd, clean)) if clean else cwd
    # Jail check
    if not abs_path.startswith(cwd):
        raise HTTPException(400, "path outside cwd")
    return cwd, abs_path


@router.get("/agents/{agent_id}/files")
async def list_files(agent_id: str, path: str = ""):
    cwd, abs_path = _resolve_agent_path(agent_id, path)
    if not os.path.isdir(abs_path):
        raise HTTPException(400, "path is not a directory")
    entries = []
    try:
        for entry in sorted(os.scandir(abs_path), key=lambda e: (e.is_file(), e.name.lower())):
            if entry.name in IGNORED_NAMES:
                continue
            if any(entry.name.endswith(s) for s in IGNORED_SUFFIXES):
                continue
            if entry.is_dir(follow_symlinks=False):
                entries.append({"name": entry.name, "type": "dir", "size": None})
            elif entry.is_file(follow_symlinks=False):
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = None
                entries.append({"name": entry.name, "type": "file", "size": size})
    except PermissionError:
        raise HTTPException(403, "permission denied")
    rel = os.path.relpath(abs_path, cwd)
    return {"cwd": cwd, "path": "" if rel == "." else rel, "entries": entries}


@router.get("/agents/{agent_id}/files/content")
async def file_content(agent_id: str, path: str = ""):
    cwd, abs_path = _resolve_agent_path(agent_id, path)
    if not os.path.isfile(abs_path):
        raise HTTPException(400, "path is not a file")
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        raise HTTPException(500, "cannot stat file")

    MAX_SIZE = 1 * 1024 * 1024  # 1 MB
    if size >= MAX_SIZE:
        from fastapi.responses import Response
        return Response(status_code=413, headers={"X-File-Size": str(size)})

    # Binary detection: read first 512 bytes, then read whole file
    try:
        with open(abs_path, "rb") as f:
            head = f.read(512)
            if b"\x00" in head:
                from fastapi.responses import Response
                return Response(status_code=415, headers={"X-File-Size": str(size)})
            rest = f.read()
        text = (head + rest).decode("utf-8", errors="replace")
    except (OSError, PermissionError) as e:
        raise HTTPException(500, str(e))

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(text, headers={"X-File-Size": str(size)})


@router.get("/agents/{agent_id}/files/download")
async def download_file(agent_id: str, path: str = ""):
    cwd, abs_path = _resolve_agent_path(agent_id, path)
    if not os.path.isfile(abs_path):
        raise HTTPException(400, "path is not a file")
    from fastapi.responses import FileResponse
    filename = os.path.basename(abs_path)
    return FileResponse(
        abs_path,
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
