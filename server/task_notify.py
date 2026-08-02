"""Feishu notification for individual task completion.

Independent of ``wiki_notify``'s digest notifications — this fires once per
task when it reaches a terminal status (success/failed), sending the task's
last agent message as a small Feishu card. Reuses ``wiki_notify.send_feishu_card``
for the actual CLI call so both pipelines share the same feishu-bot contract.
"""
from __future__ import annotations

import asyncio
import logging

from server import feishu_threads
from server.config import task_notify_config
from server.models import Task
from server.wiki_notify import send_feishu_card

logger = logging.getLogger(__name__)

# Serializes read-root → send → record per task. Two notifications for the
# same task can overlap (e.g. the task is resumed from the browser and
# finishes again while the first detached CLI call is still in flight); both
# would then read an empty root_id, send independent top-level cards, and the
# later record() would pick one arbitrarily — splitting the thread the
# reply-to-task flow depends on. Entries are ref-counted so the dict doesn't
# grow one lock per task for the process lifetime.
_send_locks: dict[str, tuple[asyncio.Lock, int]] = {}


def _acquire_send_lock(task_id: str) -> asyncio.Lock:
    lock, refs = _send_locks.get(task_id, (asyncio.Lock(), 0))
    _send_locks[task_id] = (lock, refs + 1)
    return lock


def _release_send_lock(task_id: str) -> None:
    lock, refs = _send_locks[task_id]
    if refs <= 1:
        del _send_locks[task_id]
    else:
        _send_locks[task_id] = (lock, refs - 1)


def max_queue_depth() -> int:
    """Deepest per-task send queue right now (0 when nothing is in flight).

    ``shutdown()`` sizes its drain window from this: serialized notifications
    run one CLI call after another, so N queued sends for one task can take up
    to N × the CLI's own timeout, not one timeout total.
    """
    return max((refs for _, refs in _send_locks.values()), default=0)

_STATUS_LABELS = {"success": "✅ 成功", "failed": "❌ 失败"}
_NO_OUTPUT_FALLBACK = {"success": "(任务已完成，无输出)", "failed": "(任务失败，无输出)"}
# feishu-bot CLI applies --max-len only after argv is parsed inside the
# subprocess; an oversized single argv entry can exceed the kernel's
# ARG_MAX and make process creation itself fail before that truncation
# ever runs. Trim every interpolated field, and the fully-assembled card as a
# final safety net, so the argv we hand to exec() stays small regardless of
# how long the agent's message or the agent/task names were.
_MAX_MESSAGE_CHARS = 4000
_MAX_NAME_CHARS = 200


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


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
    agent_name = _truncate(agent_name, _MAX_NAME_CHARS)
    task_name = _truncate(task_name, _MAX_NAME_CHARS)
    last_message = last_message or _NO_OUTPUT_FALLBACK.get(status, _NO_OUTPUT_FALLBACK["failed"])
    last_message = _truncate(last_message, _MAX_MESSAGE_CHARS)
    lines = [
        f"🤖 {agent_name} / {task_name}",
        f"**状态**: {_STATUS_LABELS.get(status, status)}",
        "",
        last_message,
    ]
    return "\n".join(lines)


async def notify_task_finished(agent_name: str, task: Task, start_index: int = 0) -> None:
    """Send a Feishu card with the task's last agent message, if enabled.

    Captures the sent message_id(s) and records them in ``feishu_threads``
    so a reply in the group can be resolved back to this task. If the task
    already has a recorded topic root (an earlier card), the card is sent
    with ``--reply-to`` that root so multi-round notifications collapse into
    one thread instead of starting a new one each time.
    """
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
        chat_id = cfg.get("chat_id", "")
        # Hold the per-task lock across read-send-record so an overlapping
        # notification for the same task sees the root this one established
        # and replies into it, instead of opening a second topic.
        lock = _acquire_send_lock(task.id)
        try:
            async with lock:
                # Pass chat_id so a root recorded for a previously-configured
                # group is ignored rather than replied into.
                root_id = feishu_threads.get_root_id(task.id, chat_id) or ""
                message_ids = await send_feishu_card(
                    cfg, message, capture_ids=True, reply_to=root_id
                )
                if message_ids:
                    feishu_threads.record(
                        task.id, message_ids, root_id or message_ids[0], chat_id
                    )
        finally:
            _release_send_lock(task.id)
    except Exception:
        logger.exception("task-finish feishu notification failed for task %s", task.id)
