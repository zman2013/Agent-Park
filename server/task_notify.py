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
# feishu-bot CLI applies --max-len only after argv is parsed inside the
# subprocess; an oversized single argv entry can exceed the kernel's
# ARG_MAX and make process creation itself fail before that truncation
# ever runs. Trim here so the argv we hand to exec() stays small regardless
# of how long the agent's last message was.
_MAX_MESSAGE_CHARS = 4000


def _last_agent_text(task: Task, start_index: int = 0) -> str:
    """Return the last agent text message from the CURRENT run only.

    Internal continuations (auto-continue, /compact, handoff) resume the
    subprocess without appending a user Message, so a role=="user" boundary
    can't detect them. ``start_index`` is the message count captured by the
    runner when this run started, giving an exact boundary regardless of
    whether the continuation was user- or system-initiated.
    """
    for m in reversed(task.messages[start_index:]):
        if m.role == "agent" and m.type == "text" and m.content.strip():
            return m.content.strip()
    return ""


def format_task_card(*, agent_name: str, task_name: str, status: str, last_message: str) -> str:
    last_message = last_message or _NO_OUTPUT_FALLBACK.get(status, _NO_OUTPUT_FALLBACK["failed"])
    if len(last_message) > _MAX_MESSAGE_CHARS:
        last_message = last_message[:_MAX_MESSAGE_CHARS] + "…（已截断）"
    lines = [
        f"🤖 {agent_name} / {task_name}",
        f"**状态**: {_STATUS_LABELS.get(status, status)}",
        "",
        last_message,
    ]
    return "\n".join(lines)


async def notify_task_finished(agent_name: str, task: Task, start_index: int = 0) -> None:
    """Send a Feishu card with the task's last agent message, if enabled."""
    cfg = task_notify_config()["feishu_notify"]
    if not cfg.get("enabled"):
        return
    message = format_task_card(
        agent_name=agent_name,
        task_name=task.name or task.id,
        status=task.status.value,
        last_message=_last_agent_text(task, start_index),
    )
    try:
        await send_feishu_card(cfg, message)
    except Exception:
        logger.exception("task-finish feishu notification failed for task %s", task.id)
