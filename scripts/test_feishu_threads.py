"""Smoke test for ``server.feishu_threads``.

Standalone — does not need pytest. Run with::

    .venv/bin/python scripts/test_feishu_threads.py

Sandboxes ``THREADS_FILE`` under a tmp dir and exercises record/resolve,
pruning (TTL + max-size), and corrupted-file recovery. Exits non-zero on
any assertion failure.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    sandbox = Path(tempfile.mkdtemp(prefix="ap-feishu-threads-test-"))
    try:
        import server.feishu_threads as ft

        ft.DATA_DIR = sandbox
        ft.THREADS_FILE = sandbox / "feishu_threads.json"

        # ── 1) record + resolve by parent_id / root_id / message_id ──────
        ft.record("task-1", ["om_a"], root_id="om_a", chat_id="oc_x")
        assert ft.resolve("om_a", None, None) == "task-1"
        assert ft.resolve(None, "om_a", None) == "task-1"
        assert ft.resolve(None, None, "om_a") == "task-1"
        assert ft.resolve("missing", None, None) is None
        assert ft.get_root_id("task-1") == "om_a"
        print("✓ record + resolve basics")

        # ── 2) multi-card task: several message_ids -> same task ────────
        ft.record("task-2", ["om_b1", "om_b2", "om_b3"], root_id="om_b1", chat_id="oc_x")
        for mid in ("om_b1", "om_b2", "om_b3"):
            assert ft.resolve(mid, None, None) == "task-2", mid
        data = json.loads(ft.THREADS_FILE.read_text())
        assert set(data["by_message"]) >= {"om_a", "om_b1", "om_b2", "om_b3"}
        print("✓ multi-card task all message_ids recorded")

        # ── 3) second call for same task with prior root_id -> same thread
        ft.record("task-2", ["om_b4"], root_id=ft.get_root_id("task-2"), chat_id="oc_x")
        assert ft.get_root_id("task-2") == "om_b1"
        assert ft.resolve("om_b4", None, None) == "task-2"
        print("✓ later reply collapses into same root_id")

        # ── 4) resolve priority: parent_id wins over root_id/message_id ──
        ft.record("task-3", ["om_c"], root_id="om_c", chat_id="oc_x")
        ft.record("task-4", ["om_d"], root_id="om_d", chat_id="oc_x")
        assert ft.resolve("om_c", "om_d", "om_d") == "task-3"
        print("✓ resolve priority parent_id > root_id > message_id")

        # ── 4b) a root recorded for another chat is not reused ────────────
        assert ft.get_root_id("task-3", "oc_x") == "om_c"
        assert ft.get_root_id("task-3", "oc_moved") is None, \
            "root recorded for a different chat must not be replied into"
        assert ft.get_root_id("task-3") == "om_c", "no chat_id given -> no filtering"
        print("✓ root from a different chat is discarded")

        # ── 5) empty / no-op inputs don't record anything ───────────────
        before = json.loads(ft.THREADS_FILE.read_text())
        ft.record("", ["om_e"], root_id="om_e", chat_id="oc_x")
        ft.record("task-5", [], root_id="om_e", chat_id="oc_x")
        after = json.loads(ft.THREADS_FILE.read_text())
        assert before == after, "empty task_id / no message_ids must not write"
        assert ft.resolve(None, None, None) is None
        print("✓ empty inputs are no-ops")

        # ── 6) corrupted file recovers instead of crashing ───────────────
        ft.THREADS_FILE.write_text("{not json", encoding="utf-8")
        assert ft.resolve("om_a", None, None) is None
        ft.record("task-6", ["om_f"], root_id="om_f", chat_id="oc_x")
        assert ft.resolve("om_f", None, None) == "task-6"
        print("✓ corrupted file recovers instead of crashing")

        # ── 6b) valid JSON but damaged shape also recovers. setdefault alone
        #       would keep a non-dict section, and then every resolve/record
        #       raises — i.e. recovery silently stops working.
        for broken in (
            '{"by_message": [], "by_task": []}',
            '{"by_message": "nope"}',
            '{"by_message": {"om_g": "not-a-dict"}, "by_task": {"t": 7}}',
            '[]',
        ):
            ft.THREADS_FILE.write_text(broken, encoding="utf-8")
            assert ft.resolve("om_g", None, None) is None, broken
            assert ft.get_root_id("t") is None, broken
            ft.record("task-6b", ["om_h"], root_id="om_h", chat_id="oc_x")
            assert ft.resolve("om_h", None, None) == "task-6b", broken
        print("✓ damaged-but-valid-JSON shape is normalized, not propagated")

        # ── 7) TTL pruning drops entries older than 30 days ──────────────
        stale_at = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        fresh_at = datetime.now(timezone.utc).isoformat()
        ft._save({
            "by_message": {
                "om_stale": {"task_id": "task-old", "root_id": "om_stale", "chat_id": "oc_x", "at": stale_at},
                "om_fresh": {"task_id": "task-new", "root_id": "om_fresh", "chat_id": "oc_x", "at": fresh_at},
            },
            "by_task": {
                "task-old": {"root_id": "om_stale", "chat_id": "oc_x", "at": stale_at},
                "task-new": {"root_id": "om_fresh", "chat_id": "oc_x", "at": fresh_at},
            },
        })
        ft.record("task-new", ["om_fresh2"], root_id="om_fresh", chat_id="oc_x")
        data = json.loads(ft.THREADS_FILE.read_text())
        assert "om_stale" not in data["by_message"], "TTL-expired message entry must be pruned"
        assert "task-old" not in data["by_task"], "TTL-expired task entry must be pruned"
        assert "om_fresh" in data["by_message"]
        print("✓ TTL pruning drops entries older than 30 days")

        # ── 7b) TTL also applies on READ. Pruning only runs from record(), so
        #       a mapping that never sees another notification would otherwise
        #       stay resolvable forever — the TTL would be write-only.
        ft._save({
            "by_message": {
                "om_expired": {"task_id": "task-exp", "root_id": "om_expired",
                               "chat_id": "oc_x", "at": stale_at},
            },
            "by_task": {
                "task-exp": {"root_id": "om_expired", "chat_id": "oc_x", "at": stale_at},
            },
        })
        assert ft.resolve("om_expired", None, None) is None, \
            "a reply to an expired card must not resume its task"
        assert ft.get_root_id("task-exp", "oc_x") is None, \
            "an expired topic root must not be reused"
        print("✓ TTL enforced on reads, not only on writes")

        # ── 8) max-size pruning keeps only the most recent 500 ───────────
        ft.THREADS_FILE.unlink()
        base = datetime.now(timezone.utc)
        by_message = {}
        by_task = {}
        for i in range(510):
            at = (base - timedelta(seconds=510 - i)).isoformat()
            mid = f"om_bulk{i}"
            tid = f"task-bulk{i}"
            by_message[mid] = {"task_id": tid, "root_id": mid, "chat_id": "oc_x", "at": at}
            by_task[tid] = {"root_id": mid, "chat_id": "oc_x", "at": at}
        ft._save({"by_message": by_message, "by_task": by_task})
        ft.record("task-bulk-new", ["om_bulk_new"], root_id="om_bulk_new", chat_id="oc_x")
        data = json.loads(ft.THREADS_FILE.read_text())
        assert len(data["by_message"]) == 500, len(data["by_message"])
        assert len(data["by_task"]) == 500, len(data["by_task"])
        assert "om_bulk_new" in data["by_message"], "newest entry must survive pruning"
        assert "om_bulk0" not in data["by_message"], "oldest entry must be pruned first"
        print("✓ max-size pruning keeps most recent 500")

        # ── 9) atomic write leaves no .tmp file behind ───────────────────
        assert not ft.THREADS_FILE.with_suffix(".tmp").exists()
        print("✓ no leftover .tmp file after writes")

        print("\nAll feishu_threads smoke checks passed.")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    main()
