"""Smoke test for ``server.agentloop_manager.notify_source_task``.

Standalone — does not need pytest. Run with::

    .venv/bin/python scripts/test_agentloop_notify.py

Sandboxes the registry / app_state / WebSocket broadcast under a tmp dir,
exercises the helper through six scenarios, and exits non-zero on any
assertion failure. Doubles as an integration check after refactors.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> None:
    sandbox = Path(tempfile.mkdtemp(prefix="ap-notify-test-"))

    try:
        # Patch on-disk locations BEFORE importing app_state singleton.
        import server.agentloop_manager as alm
        import server.state as state_mod
        import server.routes_ws as routes_ws
        from server.models import Agent, Message, TaskStatus  # noqa: F401
        from agentloop.workspace import WorkspacePaths

        alm.DATA_DIR = sandbox
        alm.REGISTRY_FILE = sandbox / "agentloops.json"
        state_mod.DATA_DIR = sandbox
        state_mod.AGENTS_FILE = sandbox / "agents.json"
        state_mod.TASKS_DIR = sandbox / "tasks"
        (sandbox / "tasks").mkdir(parents=True, exist_ok=True)

        broadcast_calls: list[dict] = []

        async def _fake_broadcast(msg: dict) -> None:
            broadcast_calls.append(msg)

        routes_ws.broadcast = _fake_broadcast

        # Workspace + state.json + summary.md
        project = sandbox / "project"
        project.mkdir()
        ws = WorkspacePaths.for_workspace(project, "ws-1")
        ws.workspace_dir.mkdir(parents=True, exist_ok=True)
        ws.runs_dir.mkdir(parents=True, exist_ok=True)
        ws.state_file.write_text(
            json.dumps({
                "cycle": 7,
                "total_cost_cny": 1.23,
                "exhausted_reason": None,
                "last_decision": {"next": "done"},
            }),
            encoding="utf-8",
        )
        (ws.workspace_dir / "summary.md").write_text("# done\n\nall good\n", encoding="utf-8")

        # Re-instantiate app_state so it picks up the patched paths
        state_mod.app_state = state_mod.AppState()
        agent = Agent(id="agent-1", name="test", command="cco", cwd=str(project))
        state_mod.app_state.agents["agent-1"] = agent
        state_mod.app_state._agent_order.append("agent-1")
        task = state_mod.app_state.create_task("agent-1", "trigger task")
        task.status = TaskStatus.success
        task_id = task.id

        def make_entry(loop_id: str, status: str, src: str | None = task_id) -> dict:
            return {
                "loop_id": loop_id,
                "cwd": str(project),
                "workspace": "ws-1",
                "workspace_dir": str(ws.workspace_dir),
                "design_path": str(project / "design.md"),
                "pid": 0,
                "pid_start_time": None,
                "started_at": "2026-01-01T00:00:00Z",
                "source_task_id": src,
                "status": status,
                "dismissed": False,
                "last_seen_cycle": 7,
            }

        # Always reload registry-modifying state from disk before re-saving,
        # so the test never overwrites notified_at stamps written by prior calls.
        def _set_registry(loop_ids_with_status: list[tuple[str, str, str | None]]) -> None:
            existing = {e["loop_id"]: e for e in alm._load_registry()}
            entries = []
            for lid, status, src in loop_ids_with_status:
                if lid in existing:
                    entries.append(existing[lid])
                else:
                    entries.append(make_entry(lid, status, src))
            alm._save_registry(entries)

        # ── 1) happy path ────────────────────────────────────────────────
        alm._save_registry([make_entry("L-happy", "done")])
        delivered = asyncio.run(alm.notify_source_task("L-happy"))
        assert delivered is True, "first call should deliver"
        assert len(task.messages) == 1, f"expected 1 msg, got {len(task.messages)}"
        msg = task.messages[0]
        assert msg.type == "system" and msg.role == "agent", msg
        assert "AgentLoop 已结束" in msg.content
        assert "✅ 完成" in msg.content
        assert "cycles**: 7" in msg.content
        assert "¥1.23" in msg.content
        assert "all good" in msg.content
        assert len(broadcast_calls) == 1
        assert broadcast_calls[0]["type"] == "message"
        assert broadcast_calls[0]["task_id"] == task_id
        assert alm._find("L-happy").get("notified_at"), "notified_at must be stamped"
        # Persisted on disk
        persisted = json.loads((sandbox / "tasks" / "agent-1.json").read_text())
        ptask = next(iter(persisted["tasks"].values()))
        assert any(
            m["type"] == "system" and "AgentLoop 已结束" in m["content"]
            for m in ptask["messages"]
        ), "system message must round-trip to disk"
        print("✓ happy path")

        # ── 2) idempotent ────────────────────────────────────────────────
        delivered2 = asyncio.run(alm.notify_source_task("L-happy"))
        assert delivered2 is False
        assert len(task.messages) == 1
        print("✓ idempotent")

        # ── 3) status='stopped' → never notify, never stamp ──────────────
        _set_registry([
            ("L-happy", "done", task_id),
            ("L-stopped", "stopped", task_id),
        ])
        d3 = asyncio.run(alm.notify_source_task("L-stopped"))
        assert d3 is False
        assert not alm._find("L-stopped").get("notified_at"), "stopped must not be stamped"
        print("✓ stopped skipped")

        # ── 4) source task missing → stamp + skip ────────────────────────
        _set_registry([
            ("L-happy", "done", task_id),
            ("L-stopped", "stopped", task_id),
            ("L-orphan", "done", "ghost"),
        ])
        d4 = asyncio.run(alm.notify_source_task("L-orphan"))
        assert d4 is False
        assert alm._find("L-orphan").get("notified_at"), "orphan must stamp to avoid retry"
        print("✓ missing source-task stamped + skipped")

        # ── 5) summary.md missing → header-only ─────────────────────────
        (ws.workspace_dir / "summary.md").unlink()
        _set_registry([
            ("L-happy", "done", task_id),
            ("L-stopped", "stopped", task_id),
            ("L-orphan", "done", "ghost"),
            ("L-no-summary", "done", task_id),
        ])
        d5 = asyncio.run(alm.notify_source_task("L-no-summary"))
        assert d5 is True
        last_msg = task.messages[-1]
        assert "summary.md 未生成" in last_msg.content
        assert "<details>" not in last_msg.content
        print("✓ missing summary.md → header-only")

        # ── 6) notify_pending() sweep delivers exactly one new ──────────
        (ws.workspace_dir / "summary.md").write_text("exhausted summary\n", encoding="utf-8")
        _set_registry([
            ("L-happy", "done", task_id),
            ("L-stopped", "stopped", task_id),
            ("L-orphan", "done", "ghost"),
            ("L-no-summary", "done", task_id),
            ("L-fresh", "exhausted", task_id),
        ])
        n = asyncio.run(alm.notify_pending())
        assert n == 1, f"expected 1 fresh delivery, got {n}"
        assert "⏱️ 资源耗尽" in task.messages[-1].content
        print("✓ notify_pending sweep")

        print("\nAll notify_source_task smoke checks passed.")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    main()
