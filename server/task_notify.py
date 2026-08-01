"""Feishu notification for individual task completion.

Independent of ``wiki_notify``'s digest notifications — this fires once per
task when it reaches a terminal status (success/failed), sending the task's
last agent message as a small Feishu card. Reuses ``wiki_notify.send_feishu_card``
for the actual CLI call so both pipelines share the same feishu-bot contract.
"""
from __future__ import annotations

import logging

from server.config import task_notify_config
from server.models import Task
from server.wiki_notify import send_feishu_card

logger = logging.getLogger(__name__)

_STATUS_LABELS = {"success": "✅ 成功", "failed": "❌ 失败"}
_NO_OUTPUT_FALLBACK = {"success": "(任务已完成，无输出)", "failed": "(任务失败，无输出)"}


def _last_agent_text(task: Task) -> str:
    for m in reversed(task.messages):
        if m.role == "agent" and m.type == "text" and m.content.strip():
            return m.content.strip()
    return ""


def format_task_card(*, agent_name: str, task_name: str, status: str, last_message: str) -> str:
    lines = [
        f"🤖 {agent_name} / {task_name}",
        f"**状态**: {_STATUS_LABELS.get(status, status)}",
        "",
        last_message or _NO_OUTPUT_FALLBACK.get(status, _NO_OUTPUT_FALLBACK["failed"]),
    ]
    return "\n".join(lines)


async def notify_task_finished(agent_name: str, task: Task) -> None:
    """Send a Feishu card with the task's last agent message, if enabled."""
    cfg = task_notify_config()["feishu_notify"]
    if not cfg.get("enabled"):
        return
    message = format_task_card(
        agent_name=agent_name,
        task_name=task.name or task.id,
        status=task.status.value,
        last_message=_last_agent_text(task),
    )
    try:
        await send_feishu_card(cfg, message)
    except Exception:
        logger.exception("task-finish feishu notification failed for task %s", task.id)
