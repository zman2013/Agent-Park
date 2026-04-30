"""QA agent: reviews the dev item referenced by the given qa item."""
from __future__ import annotations

from ..config import AgentBackend
from ..workspace import WorkspacePaths
from .base import RunResult, run_agent
from ._prompts import load_prompt, render


def run(ws: WorkspacePaths, item_id: str, cycle: int, backend: AgentBackend) -> RunResult:
    tpl = load_prompt("qa")
    system = render(tpl, cwd=str(ws.workspace_dir), item_id=item_id, cycle=cycle)
    prompt = (
        f"请审查 todolist.md 中 qa item {item_id} 所指向的 dev 产物。\n"
        f"工作目录：{ws.workspace_dir}\n"
        f"todolist 文件：{ws.todolist}\n"
        f"本轮 cycle：{cycle}\n\n"
        "以下是你的系统指令，严格遵守：\n\n"
        f"{system}"
    )
    return run_agent("qa", ws, item_id, backend, prompt)
