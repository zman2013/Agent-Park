"""Feishu bot notifications for the summary stage.

Independent of ``server/wiki_notify.py`` — agentloop is a standalone package
that must work in CLI mode without pulling in the server module. Keeps the
same feishu-bot CLI contract (``python3 <cli> send --chat-id ... --card ...``)
so one shared bot/chat serves both pipelines.

The loop runs synchronously, so this module uses blocking ``subprocess.run``
rather than asyncio.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .config import FeishuConfig

logger = logging.getLogger(__name__)

# Match wiki_notify's cap so feishu-bot splits long summaries into multiple
# cards instead of truncating — we don't pre-truncate the summary body.
_MAX_LEN = "3500"
_SEND_TIMEOUT_SEC = 30


def format_summary_card(
    *,
    loop_slug: str,
    project_name: str,
    design_name: str,
    exit_tag: str,
    exit_reason: str,
    cycle: int,
    total_cost_cny: float,
    summary_md: str,
) -> str:
    """Render the Feishu card markdown for a finished loop.

    The body is the full ``summary.md`` — not truncated here. feishu-bot's
    ``--max-len`` splits it into multiple cards client-side.
    """
    header = [
        f"🤖 agentloop 完成 — {loop_slug}\n",
        f"**状态**: {exit_tag}",
        f"**项目**: {project_name}",
        f"**Design**: {design_name}",
        f"**cycles**: {cycle}  **成本**: ¥{total_cost_cny:.2f}",
    ]
    if exit_reason:
        header.append(f"**退出原因**: {exit_reason}")
    return "\n".join(header) + "\n\n---\n" + summary_md.strip() + "\n"


def format_plan_review_card(
    *,
    loop_slug: str,
    project_name: str,
    design_name: str,
    items: list,
    stats: dict,
    reason: str = "",
) -> str:
    """Render the plan-review request card.

    The reviewer's job is to judge whether the oracle coverage is good enough,
    so ``checks`` (or its conspicuous absence) is the emphasized column — not
    the item titles, which always look plausible and carry little signal.
    """
    header = [
        f"⏸️ agentloop 待确认计划 — {loop_slug}\n",
        f"**项目**: {project_name}",
        f"**Design**: {design_name}",
        f"**任务**: {stats.get('items', 0)} 项"
        f"（dev {stats.get('dev', 0)} / qa {stats.get('qa', 0)}）",
    ]
    unverified = int(stats.get("unverified") or 0)
    if unverified:
        ids = ", ".join(stats.get("unverified_ids") or [])
        header.append(f"**⚠️ 无机器检查覆盖**: {unverified} 个 dev item（{ids}）")
    else:
        header.append("**检查覆盖**: 全部 dev item 均有机器检查 ✅")

    lines = []
    for it in items:
        checks = getattr(it, "checks", None)
        mark = ", ".join(checks) if checks else "⚠️ 无"
        lines.append(f"- `{it.id}` {it.type} — {it.title}")
        if it.type == "dev":
            lines.append(f"    checks: {mark}")

    footer = [
        "",
        "---",
        "在 agent-park UI 中**批准**后 loop 才会开始执行；",
        "也可以先在 UI 里直接编辑 todolist.md，批准时会绑定编辑后的版本。",
    ]
    if reason:
        footer.insert(0, f"_{reason}_")
    return "\n".join(header) + "\n\n---\n" + "\n".join(lines) + "\n" + "\n".join(footer) + "\n"


def send_feishu_card(cfg: FeishuConfig, message: str) -> bool:
    """Send ``message`` via the feishu-bot CLI.

    Returns True on success, False on any failure (missing CLI, bad chat_id,
    non-zero exit, timeout, unexpected exception). All failures are logged but
    never raised — the caller is the loop's terminal path and must not crash
    because of a notification glitch.
    """
    cli_path = cfg.cli_path
    chat_id = cfg.chat_id
    env_file = cfg.env_file

    if not cli_path or not Path(cli_path).exists():
        logger.warning(
            "Feishu CLI not found at %r, skipping summary notification", cli_path
        )
        return False
    if not chat_id:
        logger.warning("Feishu chat_id not configured, skipping summary notification")
        return False

    cmd = [
        "python3", cli_path,
        "send",
        "--chat-id", chat_id,
        "--card",
        "--max-len", _MAX_LEN,
        "--quiet",
    ]
    if env_file:
        cmd.extend(["--env-file", env_file])
    cmd.append(message)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SEND_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        logger.error("Feishu summary notification timed out after %ds", _SEND_TIMEOUT_SEC)
        return False
    except Exception:
        logger.exception("Feishu summary notification error")
        return False

    if proc.returncode == 0:
        logger.info("Feishu summary notification sent")
        return True
    logger.error(
        "Feishu summary notification failed (exit=%s): %s",
        proc.returncode, (proc.stderr or "").strip(),
    )
    return False
