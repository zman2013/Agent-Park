"""Smoke test for ``server.task_notify`` thread bookkeeping.

Standalone — does not need pytest. Run with::

    .venv/bin/python scripts/test_task_notify_threads.py

Sandboxes ``feishu_threads.THREADS_FILE`` and stubs the feishu CLI call, so
nothing is sent anywhere. Covers the root_id lifecycle: first card opens a
topic, later cards reply into it, and overlapping notifications for one task
don't split into two topics. Exits non-zero on any assertion failure.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CHAT = "oc_notify"


def main() -> None:
    sandbox = Path(tempfile.mkdtemp(prefix="ap-task-notify-test-"))
    try:
        import server.feishu_threads as ft
        import server.task_notify as tn
        from server.models import Message, Task, TaskStatus

        ft.DATA_DIR = sandbox
        ft.THREADS_FILE = sandbox / "feishu_threads.json"
        tn.task_notify_config = lambda: {
            "feishu_notify": {"enabled": True, "cli_path": "x", "chat_id": CHAT,
                              "env_file": ""},
        }

        # Record every send's reply_to so we can assert on threading, and hand
        # back a fresh message_id each time like the real CLI would.
        calls: list[str] = []
        gate: asyncio.Event | None = None

        async def fake_send(cfg, message, *, capture_ids=False, reply_to=""):
            calls.append(reply_to)
            if gate is not None:
                # Simulate the CLI being slow: yields control so a second
                # notification can interleave if nothing serializes them.
                await gate.wait()
            return [f"om_sent{len(calls)}"]

        tn.send_feishu_card = fake_send

        def make_task(task_id: str) -> Task:
            task = Task(id=task_id, agent_id="ag-1", name=f"t-{task_id}",
                        status=TaskStatus.success)
            task.messages.append(Message(role="agent", content="done"))
            return task

        # ── 1) first card opens a topic; the second replies into it ───────
        task = make_task("task-thread")
        asyncio.run(tn.notify_task_finished("tester", task, 0))
        assert calls == [""], calls
        root = ft.get_root_id("task-thread", CHAT)
        assert root == "om_sent1", root

        asyncio.run(tn.notify_task_finished("tester", task, 0))
        assert calls == ["", "om_sent1"], calls
        assert ft.get_root_id("task-thread", CHAT) == "om_sent1", \
            "root must stay the first card, not move to the latest"
        print("✓ first card opens a topic, later cards reply into it")

        # ── 2) overlapping notifications for one task share one topic. Both
        #      run while the first CLI call is still in flight — without a
        #      per-task lock both read an empty root and open two topics.
        calls.clear()
        task2 = make_task("task-overlap")

        async def overlap():
            nonlocal gate
            gate = asyncio.Event()
            first = asyncio.create_task(tn.notify_task_finished("tester", task2, 0))
            second = asyncio.create_task(tn.notify_task_finished("tester", task2, 0))
            await asyncio.sleep(0)  # let both reach the lock / the send
            gate.set()
            await asyncio.gather(first, second)
            gate = None

        asyncio.run(overlap())
        assert len(calls) == 2, calls
        assert calls[0] == "", calls
        assert calls[1] == "om_sent1", \
            f"second card must reply into the first card's topic, got {calls}"
        assert ft.get_root_id("task-overlap", CHAT) == "om_sent1"
        print("✓ overlapping notifications collapse into one topic")

        # ── 3) the per-task lock dict doesn't leak entries ────────────────
        assert tn._send_locks == {}, tn._send_locks
        print("✓ send-lock table is empty once notifications finish")

        # ── 4) disabled config sends nothing ─────────────────────────────
        calls.clear()
        tn.task_notify_config = lambda: {"feishu_notify": {"enabled": False}}
        asyncio.run(tn.notify_task_finished("tester", make_task("task-off"), 0))
        assert calls == [], calls
        print("✓ disabled config sends nothing")

        print("\nAll task_notify thread checks passed.")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    main()
