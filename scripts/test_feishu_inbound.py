"""Smoke test for the ``/api/feishu/inbound`` decision logic.

Standalone — does not need pytest (nor httpx, so no HTTP layer is involved:
the endpoint coroutine is awaited directly, which is where all the logic
lives). Run with::

    .venv/bin/python scripts/test_feishu_inbound.py

The inbound config, the thread mapping file and ``runner.send_input`` are all
stubbed so nothing touches the live state dir or spawns a subprocess. Exits
non-zero on any assertion failure.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CHAT = "oc_allowed"


def main() -> None:
    sandbox = Path(tempfile.mkdtemp(prefix="ap-feishu-inbound-test-"))
    try:
        import server.feishu_threads as ft
        import server.routes_rest as rr
        import server.routes_ws as rws
        from server.agent_runner import runner
        from server.models import Agent, Task, TaskStatus
        from server.state import app_state

        ft.DATA_DIR = sandbox
        ft.THREADS_FILE = sandbox / "feishu_threads.json"
        rr.task_notify_config = lambda: {"inbound": {"enabled": True, "chat_id": CHAT}}

        # Stub the side effects: no subprocess, no disk writes, no WS clients.
        sent: list[tuple[str, str]] = []

        async def fake_send_input(task_id, user_input, **kw):
            sent.append((task_id, user_input))
            app_state.get_task(task_id).status = TaskStatus.running

        async def fake_broadcast(payload):
            # Yields control, which is exactly what let the race slip through
            # before the claim was moved ahead of this await.
            await asyncio.sleep(0)

        runner.send_input = fake_send_input
        rws.broadcast = fake_broadcast
        app_state.save_agent_tasks = lambda agent_id: None

        agent = Agent(id="ag-1", name="tester", working_dir=str(sandbox))
        app_state.agents[agent.id] = agent

        def make_task(task_id: str, status: TaskStatus) -> Task:
            task = Task(id=task_id, agent_id=agent.id, name=f"t-{task_id}", status=status)
            app_state.tasks[task_id] = task
            return task

        def call(**kw) -> dict:
            body = rr.FeishuInboundBody(**{
                "chat_id": CHAT, "sender_open_id": "ou_human",
                "sender_type": "user", "text": "继续", **kw,
            })
            return asyncio.run(rr.feishu_inbound(body))

        # ── 1) happy path: reply to a recorded card resumes the task ─────
        make_task("task-ok", TaskStatus.success)
        ft.record("task-ok", ["om_ok"], root_id="om_ok", chat_id=CHAT)
        res = call(parent_id="om_ok", text="接着改")
        assert res["action"] == "resumed", res
        assert res["task_id"] == "task-ok", res
        assert sent == [("task-ok", "接着改")], sent
        assert app_state.get_task("task-ok").messages[-1].content == "接着改"
        print("✓ recorded reply resumes the task")

        # ── 2) unmatched reply gets a hint, no send_input ────────────────
        sent.clear()
        res = call(parent_id="om_nope")
        assert res == {"matched": False, "hint": "请回复 task 卡片以继续该任务"}, res
        assert sent == []
        print("✓ unmatched reply hints without resuming")

        # ── 3) P1 regression: app messages are ignored BEFORE lookup, and
        #      carry no `hint` — else the bot answers its own answer forever.
        #      Checked for both an unmapped and a mapped parent_id.
        for kw in ({"parent_id": "om_nope"}, {"parent_id": "om_ok"}):
            res = call(sender_type="app", **kw)
            assert res["action"] == "ignored", res
            assert res["reason"] == "app_message", res
            assert "hint" not in res, f"app message must get no hint: {res}"
        assert sent == [], "app message must never resume a task"
        print("✓ app messages ignored pre-lookup with no hint (no feedback loop)")

        # ── 4) wrong chat_id is rejected ─────────────────────────────────
        make_task("task-chat", TaskStatus.success)
        ft.record("task-chat", ["om_chat"], root_id="om_chat", chat_id="oc_other")
        res = call(parent_id="om_chat", chat_id="oc_other")
        assert res["reason"] == "chat_not_allowed", res
        assert sent == []
        print("✓ non-whitelisted chat_id rejected")

        # ── 5) running task is rejected ──────────────────────────────────
        make_task("task-run", TaskStatus.running)
        ft.record("task-run", ["om_run"], root_id="om_run", chat_id=CHAT)
        res = call(parent_id="om_run")
        assert res["reason"] == "task_running", res
        assert sent == []
        print("✓ running task rejected")

        # ── 6) P2 regression: concurrent replies to one task -> exactly one
        #      resume. The claim must land before the first await, else both
        #      pass the guard and the loser's send_input kills the winner's
        #      subprocess, dropping the winner's input.
        sent.clear()
        make_task("task-race", TaskStatus.success)
        ft.record("task-race", ["om_race"], root_id="om_race", chat_id=CHAT)

        async def race() -> list[dict]:
            def body(text: str) -> rr.FeishuInboundBody:
                return rr.FeishuInboundBody(
                    parent_id="om_race", chat_id=CHAT, sender_open_id="ou_human",
                    sender_type="user", text=text,
                )
            return list(await asyncio.gather(
                rr.feishu_inbound(body("first")),
                rr.feishu_inbound(body("second")),
            ))

        results = asyncio.run(race())
        actions = sorted(r.get("action", "") for r in results)
        assert actions == ["rejected", "resumed"], results
        assert len(sent) == 1, f"exactly one resume expected, got {sent}"
        rejected = next(r for r in results if r.get("action") == "rejected")
        assert rejected["reason"] == "task_running", rejected
        print("✓ concurrent replies serialize to a single resume")

        # ── 7) cross-path race: a browser user_message over WS and a feishu
        #      reply for the same task must serialize. Both append a Message
        #      and await before send_input() flips the status, so a REST-only
        #      reservation wouldn't cover this pair.
        #
        #      Note the two paths are deliberately asymmetric: WS sends
        #      unconditionally (a human typing in the browser interrupts the
        #      current run — pre-existing `kill_existing=True` semantics),
        #      while a feishu reply defers to a running task. So the invariant
        #      is "no interleaving", not "always exactly one send".
        def cross_case(order_ws_first: bool):
            sent.clear()
            tid = f"task-cross-{'ws' if order_ws_first else 'rest'}"
            make_task(tid, TaskStatus.success)
            mid = f"om_{tid}"
            ft.record(tid, [mid], root_id=mid, chat_id=CHAT)

            async def run():
                reply = rr.feishu_inbound(rr.FeishuInboundBody(
                    parent_id=mid, chat_id=CHAT, sender_open_id="ou_human",
                    sender_type="user", text="from feishu",
                ))
                ws_msg = rws._handle_client_message(
                    {"type": "user_message", "task_id": tid, "content": "from browser"},
                    None,
                )
                if order_ws_first:
                    _, res = await asyncio.gather(ws_msg, reply)
                else:
                    res, _ = await asyncio.gather(reply, ws_msg)
                return res

            return tid, asyncio.run(run())

        # 7a) WS wins the lock -> the feishu reply must back off, not clobber
        #     the subprocess the browser message just started.
        tid, res = cross_case(order_ws_first=True)
        assert res.get("action") == "rejected", res
        assert res["reason"] == "task_running", res
        assert sent == [(tid, "from browser")], sent
        print("✓ WS-first: feishu reply backs off instead of killing the WS run")

        # 7b) feishu wins -> the browser message still interrupts (existing
        #     WS semantics), but only after the reply's send_input completed —
        #     the two never overlap.
        tid, res = cross_case(order_ws_first=False)
        assert res.get("action") == "resumed", res
        assert sent == [(tid, "from feishu"), (tid, "from browser")], sent
        print("✓ feishu-first: WS interrupt is serialized after, not interleaved")

        # ── 8) trigger_compact / trigger_handoff take the same lock. Both are
        #      "reject while running" like a feishu reply, so racing one
        #      against a reply must leave exactly one run started.
        for cmd in ("trigger_compact", "trigger_handoff"):
            sent.clear()
            tid = f"task-{cmd}"
            make_task(tid, TaskStatus.success)
            mid = f"om_{tid}"
            ft.record(tid, [mid], root_id=mid, chat_id=CHAT)

            async def run(cmd=cmd, tid=tid, mid=mid):
                reply = rr.feishu_inbound(rr.FeishuInboundBody(
                    parent_id=mid, chat_id=CHAT, sender_open_id="ou_human",
                    sender_type="user", text="from feishu",
                ))
                ws_cmd = rws._handle_client_message({"type": cmd, "task_id": tid}, None)
                res, _ = await asyncio.gather(reply, ws_cmd)
                return res

            res = asyncio.run(run())
            assert res.get("action") == "resumed", (cmd, res)
            assert sent == [(tid, "from feishu")], \
                f"{cmd} must back off, not start a second run: {sent}"
            print(f"✓ {cmd} serializes on the shared lock and backs off")

        print("\nAll feishu inbound smoke checks passed.")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    main()
