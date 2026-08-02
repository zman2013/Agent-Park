"""message_id <-> task_id mapping for the feishu reply-to-task flow.

Single writer: agent-park (via ``task_notify.py`` after sending a card). The
feishu bot never reads or writes this file directly — it only calls the
``/api/feishu/inbound`` REST endpoint, which uses ``resolve()`` on its
behalf. This keeps the mapping a single source of truth.

On-disk shape (``data/feishu_threads.json``)::

    {
      "by_message": {"om_a": {"task_id": "abc", "root_id": "om_a",
                               "chat_id": "oc_x", "at": "…"}},
      "by_task":    {"abc":  {"root_id": "om_a", "chat_id": "oc_x", "at": "…"}}
    }

A task may have several message_ids (a long message gets split into
multiple cards by ``--max-len``) — all of them map to the same task via
``by_message``, while ``by_task`` tracks the current topic root so later
cards for the same task can be sent with ``--reply-to`` and collapse into
one thread.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
THREADS_FILE = DATA_DIR / "feishu_threads.json"

_MAX_ENTRIES = 500
_TTL_DAYS = 30

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_at(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _empty() -> dict:
    return {"by_message": {}, "by_task": {}}


def _load() -> dict:
    if not THREADS_FILE.exists():
        return _empty()
    try:
        data = json.loads(THREADS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load feishu_threads.json; starting fresh")
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    # Normalize the shape, don't just fill in missing keys: a file that is
    # valid JSON but damaged (e.g. `{"by_message": []}`, or an entry that is a
    # string) would otherwise survive setdefault and blow up later inside
    # resolve()/record(), turning "recover from a corrupt file" into "every
    # reply and notification fails from now on".
    for section in ("by_message", "by_task"):
        entries = data.get(section)
        if not isinstance(entries, dict):
            data[section] = {}
            continue
        data[section] = {
            k: v for k, v in entries.items()
            if isinstance(k, str) and isinstance(v, dict)
        }
    return data


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = THREADS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(THREADS_FILE)


def _prune(data: dict) -> dict:
    """Drop entries older than the TTL, then cap each section at the max size."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_TTL_DAYS)
    for section in ("by_message", "by_task"):
        entries = data.get(section, {})
        kept = {k: v for k, v in entries.items() if _parse_at(v.get("at")) >= cutoff}
        if len(kept) > _MAX_ENTRIES:
            newest = sorted(kept.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)
            kept = dict(newest[:_MAX_ENTRIES])
        data[section] = kept
    return data


def record(task_id: str, message_ids: Iterable[str], root_id: str, chat_id: str) -> None:
    """Record message_id(s) -> task_id and task_id -> root_id mappings.

    ``root_id`` is the topic root subsequent replies should target — callers
    pass the first-ever message_id when a task has no recorded root yet, or
    the previously recorded root on later cards so multi-round replies
    collapse into the same thread.
    """
    message_ids = [m for m in message_ids if m]
    if not task_id or not message_ids:
        return
    at = _now()
    with _LOCK:
        data = _load()
        for mid in message_ids:
            data["by_message"][mid] = {
                "task_id": task_id,
                "root_id": root_id,
                "chat_id": chat_id,
                "at": at,
            }
        data["by_task"][task_id] = {"root_id": root_id, "chat_id": chat_id, "at": at}
        data = _prune(data)
        _save(data)


def get_root_id(task_id: str, chat_id: str = "") -> str | None:
    """Return the recorded topic root for ``task_id``, or ``None`` if absent.

    When ``chat_id`` is given, a root recorded for a different chat counts as
    absent: after the configured destination group changes, the stored root is
    a message in a chat we no longer post to, so replying to it would either
    fail or land the card back in the old conversation. Returning None makes
    the next card start a fresh thread in the new group instead.
    """
    with _LOCK:
        data = _load()
    entry = data["by_task"].get(task_id)
    if not entry:
        return None
    if chat_id and entry.get("chat_id") and entry["chat_id"] != chat_id:
        return None
    return entry.get("root_id") or None


def resolve(parent_id: str | None, root_id: str | None, message_id: str | None) -> str | None:
    """Reverse-lookup a task_id from an inbound reply's message ids.

    Tries ``parent_id`` (the message actually being replied to) first, then
    ``root_id`` (in case the parent itself was pruned but the thread root
    survives), then ``message_id`` itself. Returns ``None`` if none resolve.
    """
    with _LOCK:
        data = _load()
    for mid in (parent_id, root_id, message_id):
        if not mid:
            continue
        entry = data["by_message"].get(mid)
        if entry and entry.get("task_id"):
            return entry["task_id"]
    return None
