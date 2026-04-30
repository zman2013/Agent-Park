"""Planner agent: runs exactly once at the start of the loop."""
from __future__ import annotations

from ..config import AgentBackend
from ..workspace import WorkspacePaths
from .base import RunResult, run_agent
from ._prompts import load_prompt


def run(ws: WorkspacePaths, backend: AgentBackend) -> RunResult:
    prompt_tpl = load_prompt("planner")
    # planner has no variables; template is appended to a short direct prompt
    prompt = (
        f"请根据 {ws.design} 生成初始 todolist.md（写入 {ws.todolist}）。\n\n"
        "以下是你的系统指令，严格遵守：\n\n"
        f"{prompt_tpl}"
    )
    return run_agent("planner", ws, None, backend, prompt)
