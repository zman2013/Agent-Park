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
    archived: bool | None = None


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
    if body.archived is not None:
        agent.archived = body.archived
        changed["archived"] = agent.archived
        if agent.archived and agent_id in app_state._agent_order:
            app_state._agent_order.remove(agent_id)
        elif not agent.archived and agent_id not in app_state._agent_order:
            app_state._agent_order.append(agent_id)
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


@router.post("/agents/{agent_id}/archive")
async def archive_agent(agent_id: str):
    agent = app_state.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    try:
        app_state.archive_agent(agent_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    from server.routes_ws import broadcast, agent_updated_message
    await broadcast(agent_updated_message(agent, {"archived": True}))
    return agent.model_dump()


@router.post("/agents/{agent_id}/unarchive")
async def unarchive_agent(agent_id: str):
    agent = app_state.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    try:
        app_state.unarchive_agent(agent_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    from server.routes_ws import broadcast, agent_updated_message
    await broadcast(agent_updated_message(agent, {"archived": False}))
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


# ── Knowledge endpoints ────────────────────────────────────────────────────────

@router.get("/agents/{agent_id}/knowledge")
async def get_knowledge(agent_id: str):
    """Return the three knowledge documents for an agent."""
    if agent_id not in app_state.agents:
        raise HTTPException(404, "agent not found")
    from server.knowledge import read_knowledge_docs
    return read_knowledge_docs(agent_id)


# ── Prompts endpoints ─────────────────────────────────────────────────────────

class PromptAddBody(BaseModel):
    title: str = ""
    content: str


@router.get("/prompts")
async def get_prompts():
    from server.prompts import list_prompts
    return list_prompts()


@router.post("/prompts")
async def add_prompt(body: PromptAddBody):
    if not body.content.strip():
        raise HTTPException(400, "content is required")
    from server.prompts import append_prompt
    return append_prompt(body.title.strip(), body.content)


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(prompt_id: str):
    from server.prompts import delete_prompt as _delete
    if not _delete(prompt_id):
        raise HTTPException(404, "prompt not found")
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


def _resolve_agent_path(agent_id: str, rel_path: str) -> tuple[str, str, bool]:
    """Return (cwd, abs_path, is_symlink_escape) or raise HTTPException.

    Unlike the strict jail check, paths that escape cwd via symlinks are
    allowed — the bool flag indicates when this happens so callers can
    decide whether to enforce a boundary.
    """
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
    # Use normpath instead of realpath to NOT resolve symlinks
    abs_path_logical = os.path.normpath(os.path.join(cwd, clean)) if clean else cwd
    abs_path = os.path.realpath(abs_path_logical) if clean else cwd
    # Jail check on logical path (before symlink resolution)
    if not os.path.commonpath([abs_path_logical, cwd]) == cwd:
        raise HTTPException(400, "path outside cwd")
    is_symlink_escape = (abs_path != abs_path_logical)
    return cwd, abs_path, is_symlink_escape


@router.get("/agents/{agent_id}/files")
async def list_files(agent_id: str, path: str = ""):
    cwd, abs_path, _ = _resolve_agent_path(agent_id, path)
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
                entries.append({"name": entry.name, "type": "dir", "size": None, "is_symlink": False})
            elif entry.is_file(follow_symlinks=False):
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = None
                entries.append({"name": entry.name, "type": "file", "size": size, "is_symlink": False})
            elif entry.is_symlink():
                try:
                    resolved = os.path.realpath(entry.path)
                    if os.path.isdir(resolved):
                        entries.append({"name": entry.name, "type": "dir", "size": None, "is_symlink": True})
                    elif os.path.isfile(resolved):
                        try:
                            size = os.stat(resolved).st_size
                        except OSError:
                            size = None
                        entries.append({"name": entry.name, "type": "file", "size": size, "is_symlink": True})
                except OSError:
                    pass  # broken symlink
    except PermissionError:
        raise HTTPException(403, "permission denied")
    rel = os.path.relpath(abs_path, cwd)
    # When the resolved path escapes cwd via symlink, use the absolute path
    path_field = "" if rel == "." else rel
    if os.path.isabs(path_field) or not abs_path.startswith(cwd):
        path_field = abs_path
    return {"cwd": cwd, "path": path_field, "entries": entries}


@router.get("/agents/{agent_id}/files/search")
async def search_files(agent_id: str, q: str = "", limit: int = 50):
    """Recursive file search by name substring within agent cwd.

    Uses the system `find` command for performance on large repos.
    """
    if not q or len(q) < 1:
        return {"cwd": "", "query": q, "results": []}

    agent = app_state.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    if not agent.cwd:
        raise HTTPException(400, "agent has no cwd configured")
    cwd = os.path.realpath(agent.cwd)
    if not os.path.isdir(cwd):
        raise HTTPException(400, "agent cwd does not exist")

    limit = min(limit, 100)

    # Build find command with pruning for ignored dirs
    # -prune stops descending into ignored directories at kernel level
    prune_names = list(IGNORED_NAMES)
    prune_expr = " -o ".join(f'-name {n}' for n in prune_names)
    # -iname '*q*' does case-insensitive glob match
    # find outputs: type_char<TAB>relative_path  (via -printf)
    import subprocess, shlex
    cmd = (
        f'find . '
        f'\\( {prune_expr} \\) -prune '
        f'-o \\( -iname {shlex.quote("*" + q + "*")} \\) -printf "%y\\t%P\\n"'
    )

    try:
        proc = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(
                cmd, shell=True, cwd=cwd,
                capture_output=True, text=True, timeout=5,
            ),
        )
    except Exception:
        return {"cwd": cwd, "query": q, "results": []}

    results = []
    for line in proc.stdout.splitlines():
        if not line or '\t' not in line:
            continue
        type_char, rel_path = line.split('\t', 1)
        if not rel_path:
            continue
        name = os.path.basename(rel_path)
        if any(name.endswith(s) for s in IGNORED_SUFFIXES):
            continue
        entry_type = "dir" if type_char == "d" else "file"
        size = None
        if entry_type == "file":
            try:
                size = os.path.getsize(os.path.join(cwd, rel_path))
            except OSError:
                pass
        results.append({"name": name, "path": rel_path, "type": entry_type, "size": size})
        if len(results) >= limit:
            break

    return {"cwd": cwd, "query": q, "results": results}


@router.get("/agents/{agent_id}/files/resolve")
async def resolve_file_path(agent_id: str, path: str = ""):
    """Resolve a relative or absolute path and return file info if it exists.

    Supports:
    - Relative paths: "src/main.py" → resolved against agent cwd
    - Absolute paths: "/home/user/project/src/main.py" → checked if within cwd

    Returns file/directory info if exists, or exists=false if not found.
    """
    if not path or not path.strip():
        return {"exists": False, "error": "path is required"}

    agent = app_state.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    if not agent.cwd:
        raise HTTPException(400, "agent has no cwd configured")

    cwd = os.path.realpath(agent.cwd)
    if not os.path.isdir(cwd):
        raise HTTPException(400, "agent cwd does not exist")

    input_path = path.strip()

    # Handle absolute/relative path — absolute paths are allowed to point anywhere
    # so that symlink targets outside cwd can be navigated
    if os.path.isabs(input_path):
        abs_path = os.path.realpath(input_path)
    else:
        # Relative path: resolve against cwd
        # Remove leading ./ if present
        clean = input_path.lstrip("./")
        abs_path = os.path.realpath(os.path.join(cwd, clean))

    # For relative paths that resolve via symlink outside cwd,
    # still allow access (use the resolved absolute path)
    if not os.path.isabs(input_path):
        try:
            if os.path.commonpath([cwd, abs_path]) != cwd:
                # Symlink escapes cwd — return absolute path so frontend can navigate
                pass  # allow, will return abs_path below
        except ValueError:
            pass  # different mount, still allow

    # Check if path exists
    if not os.path.exists(abs_path):
        return {"exists": False, "path": input_path, "cwd": cwd}

    # Get file info
    is_dir = os.path.isdir(abs_path)
    is_file = os.path.isfile(abs_path)

    # Use abs_path when symlink target is outside cwd (relpath would be nonsense)
    try:
        rel_path = os.path.relpath(abs_path, cwd)
        use_path = rel_path if rel_path != "." and not rel_path.startswith("..") else abs_path
    except ValueError:
        use_path = abs_path

    result = {
        "exists": True,
        "path": use_path,
        "abs_path": abs_path,
        "cwd": cwd,
        "type": "dir" if is_dir else "file" if is_file else "other",
    }

    if is_file:
        try:
            result["size"] = os.path.getsize(abs_path)
        except OSError:
            result["size"] = None

    return result


@router.get("/agents/{agent_id}/files/content")
async def file_content(agent_id: str, path: str = ""):
    cwd, abs_path, _ = _resolve_agent_path(agent_id, path)
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
    cwd, abs_path, _ = _resolve_agent_path(agent_id, path)
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
