"""Planner agent: runs exactly once at the start of the loop."""
from __future__ import annotations

from pathlib import Path

from ..config import AgentBackend
from ..plan_review import rejection_note_path
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

    When a previous plan was rejected, ``plan-rejection.md`` holds the
    reviewer's reason and is injected into the prompt — without it a ``--fresh``
    re-plan regenerates the same rejected plan verbatim.
    """
    prompt_tpl = load_prompt("planner")
    # planner has no variables; template is appended to a short direct prompt
    prompt = (
        f"请根据 {design_path} 生成初始 todolist.md（写入 {ws.todolist}）。\n"
        f"工作目录：{ws.workspace_dir}\n\n"
        f"{_rejection_feedback(ws)}"
        "以下是你的系统指令，严格遵守：\n\n"
        f"{prompt_tpl}"
    )
    return run_agent("planner", ws, None, backend, prompt)


def _rejection_feedback(ws: WorkspacePaths) -> str:
    """The previous rejection note as a prompt section, or "" when absent."""
    path = rejection_note_path(ws)
    try:
        note = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not note:
        return ""
    return (
        "上一版计划被人工评审驳回，以下是驳回意见。新计划必须针对性地解决这些"
        "问题，不要重复提交同样的方案：\n\n"
        f"{note}\n\n"
    )
