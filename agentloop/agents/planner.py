"""Planner agent: runs exactly once at the start of the loop."""
from __future__ import annotations

from pathlib import Path

from ..config import AgentBackend
from ..workspace import WorkspacePaths
from .base import RunResult, run_agent
from ._prompts import load_prompt


def run(
    ws: WorkspacePaths, backend: AgentBackend, design_path: Path
) -> RunResult:
    """Plan the initial todolist.

    ``design_path`` is the actual design.md the CLI was invoked against. We
    cannot rely on ``ws.design`` because it's only populated by the
    agent-park manager (which symlinks design.md into the workspace); plain
    ``agentloop run foo.md`` leaves it absent.
    """
    prompt_tpl = load_prompt("planner")
    # planner has no variables; template is appended to a short direct prompt
    prompt = (
        f"请根据 {design_path} 生成初始 todolist.md（写入 {ws.todolist}）。\n"
        f"工作目录：{ws.workspace_dir}\n\n"
        "以下是你的系统指令，严格遵守：\n\n"
        f"{prompt_tpl}"
    )
    return run_agent("planner", ws, None, backend, prompt)
