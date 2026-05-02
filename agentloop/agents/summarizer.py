"""Summary agent: runs exactly once at the end of the loop.

Unlike planner/dev/qa, the summarizer is *terminal* — it runs on every exit
path (SUCCESS / PARTIAL_SUCCESS / EXHAUSTED / ERROR) and never re-enters the
scheduler. Its only permitted write is ``summary.md`` in the workspace root.
"""
from __future__ import annotations

from pathlib import Path

from ..config import AgentBackend
from ..workspace import WorkspacePaths
from .base import RunResult, run_agent
from ._prompts import load_prompt, render


def run(
    ws: WorkspacePaths,
    backend: AgentBackend,
    *,
    exit_tag: str,
    exit_reason: str,
    cycle: int,
    total_cost_cny: float,
) -> RunResult:
    """Generate ``<workspace>/summary.md`` based on the loop's final state.

    Inputs for the LLM come from files already on disk (design.md, todolist.md,
    state.json, runs/*.jsonl) — we don't re-serialize them in the prompt to
    keep this thin. The prompt template tells the agent what to read and what
    shape the output should take.
    """
    summary_path = ws.workspace_dir / "summary.md"
    prompt_tpl = load_prompt("summary")
    body = render(
        prompt_tpl,
        workspace_dir=str(ws.workspace_dir),
        summary_path=str(summary_path),
        design_path=str(ws.design),
        todolist_path=str(ws.todolist),
        state_path=str(ws.state_file),
        runs_dir=str(ws.runs_dir),
        exit_tag=exit_tag,
        exit_reason=exit_reason,
        cycle=cycle,
        total_cost_cny=f"{total_cost_cny:.2f}",
    )
    return run_agent("summary", ws, None, backend, body)


def write_fallback(
    ws: WorkspacePaths,
    *,
    exit_tag: str,
    exit_reason: str,
    cycle: int,
    total_cost_cny: float,
    failure_note: str = "",
) -> Path:
    """Write a minimal summary.md when the LLM summarizer failed.

    Ensures the UI / Feishu always have *something* to display, sourced from
    the structured state we already have in memory. The fallback is crude —
    no narrative, just the facts — but distinguishes "loop finished but
    summarizer broke" from "nothing ran at all".
    """
    summary_path = ws.workspace_dir / "summary.md"
    lines = [
        "# agentloop summary (fallback)",
        "",
        f"- **退出状态**: {exit_tag}",
        f"- **退出原因**: {exit_reason or '(无)'}",
        f"- **cycles**: {cycle}",
        f"- **总成本**: ¥{total_cost_cny:.2f}",
    ]
    if failure_note:
        lines += ["", f"> summarizer agent 失败：{failure_note}"]
    lines += [
        "",
        "> 由 fallback 生成，未调用 LLM。如需完整总结请手工查看",
        f"> `todolist.md` / `state.json` / `runs/` 位于：`{ws.workspace_dir}`。",
        "",
    ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path
