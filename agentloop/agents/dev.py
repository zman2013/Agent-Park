"""Dev agent: implements one assigned todolist item."""
from __future__ import annotations

from pathlib import Path

from ..config import AgentBackend
from .base import RunResult, run_agent
from ._prompts import load_prompt, render


def run(cwd: Path, item_id: str, cycle: int, backend: AgentBackend) -> RunResult:
    tpl = load_prompt("dev")
    system = render(tpl, cwd=str(cwd), item_id=item_id, cycle=cycle)
    prompt = (
        f"请完成 todolist.md 中的 item {item_id}。\n"
        f"工作目录：{cwd}\n"
        f"本轮 cycle：{cycle}\n\n"
        "以下是你的系统指令，严格遵守：\n\n"
        f"{system}"
    )
    return run_agent("dev", cwd, item_id, backend, prompt)
