"""Tests for agentloop_manager's plan-review integration.

Focus on the two things the manager owns that the agentloop package cannot
test: deriving ``awaiting_review`` status for a loop that exited at the gate,
and the approve path that relaunches the process.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import plan_review
from agentloop.workspace import WorkspacePaths
from server import agentloop_manager as m


TODOLIST = """---
project: demo
design_doc: design.md
created_at: 2026-09-05T00:00:00Z
cycle: 0
---

# Todolist

## Items

### T-001 · type:dev · status:pending
Implement the thing
- dependencies: []
"""


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspacePaths:
    ws = WorkspacePaths.for_workspace(tmp_path, "mgr-ws")
    ws.workspace_dir.mkdir(parents=True, exist_ok=True)
    ws.todolist.write_text(TODOLIST, encoding="utf-8")
    design = tmp_path / "design.md"
    design.write_text("# Design\n", encoding="utf-8")
    ws.design.symlink_to(design)
    return ws


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the manager's registry at a temp file."""
    reg = tmp_path / "agentloops.json"
    monkeypatch.setattr(m, "REGISTRY_FILE", reg)
    return reg


# ---------- status derivation ---------------------------------------------


def test_awaiting_gate_derives_awaiting_review(workspace):
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    # No exhausted_reason, no done decision — pre-gate this looked "stopped".
    state = {"cycle": 0, "last_decision": None}
    assert m._derive_status_from_state(state, workspace) == "awaiting_review"


def test_rejected_gate_derives_plan_rejected(workspace):
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    plan_review.reject(workspace, note="拆细一点")
    state = {"cycle": 0, "last_decision": None}
    assert m._derive_status_from_state(state, workspace) == "plan_rejected"


def test_consumed_gate_does_not_mask_real_status(workspace):
    """Once approved+consumed, normal status derivation must resume."""
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    plan_review.approve(workspace)
    plan_review.consume(workspace)
    state = {"cycle": 12, "last_decision": {"next": "done"}}
    assert m._derive_status_from_state(state, workspace) == "done"


def test_no_gate_file_preserves_legacy_derivation(workspace):
    state = {"cycle": 3, "exhausted_reason": "max_cycles reached (60)"}
    assert m._derive_status_from_state(state, workspace) == "exhausted"
    assert m._derive_status_from_state(state) == "exhausted"


def test_awaiting_review_is_notifiable():
    """The gate is the one status that *requires* human action — must notify."""
    assert "awaiting_review" in m._NOTIFIABLE_STATUSES
    assert "awaiting_review" in m._PAUSED_STATUSES


# ---------- review_plan ---------------------------------------------------


def _entry(workspace: WorkspacePaths, tmp_path: Path) -> dict:
    return {
        "loop_id": "loop-1",
        "cwd": str(tmp_path),
        "workspace": workspace.slug,
        "workspace_dir": str(workspace.workspace_dir),
        "design_path": str(tmp_path / "design.md"),
        "pid": 0,
        "pid_start_time": None,
        "started_at": "2026-09-05T00:00:00Z",
        "source_task_id": "task-9",
        "status": "awaiting_review",
        "dismissed": False,
        "last_seen_cycle": 0,
        "notified_at": "2026-09-05T00:01:00Z",
    }


def test_approve_relaunches_and_binds_digest(
    workspace, registry, tmp_path, monkeypatch
):
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    m._upsert(_entry(workspace, tmp_path))

    started: list[dict] = []

    def fake_start(**kwargs):
        started.append(kwargs)
        return {**_entry(workspace, tmp_path), "status": "running", "pid": 4242}

    monkeypatch.setattr(m, "start", fake_start)

    out = m.review_plan("loop-1", approve=True)

    assert out["status"] == "running"
    assert len(started) == 1
    assert started[0]["workspace"] == workspace.slug, "must reuse the same workspace"
    assert started[0]["source_task_id"] == "task-9"
    review = plan_review.PlanReview.load(workspace)
    assert review.state == plan_review.APPROVED
    assert review.todolist_digest == plan_review.todolist_digest(workspace)


def test_approve_with_edited_todolist_binds_edited_content(
    workspace, registry, tmp_path, monkeypatch
):
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    m._upsert(_entry(workspace, tmp_path))
    monkeypatch.setattr(m, "start", lambda **kw: {"status": "running"})

    edited = TODOLIST.replace("Implement the thing", "Implement it, scoped down")
    m.review_plan("loop-1", approve=True, todolist=edited)

    assert "scoped down" in workspace.todolist.read_text(encoding="utf-8")
    review = plan_review.PlanReview.load(workspace)
    assert review.todolist_digest == plan_review.todolist_digest(workspace)


def test_unparseable_edit_is_rejected_without_clobbering(
    workspace, registry, tmp_path, monkeypatch
):
    """A bad edit must not destroy the existing plan."""
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    m._upsert(_entry(workspace, tmp_path))
    monkeypatch.setattr(m, "start", lambda **kw: {"status": "running"})

    with pytest.raises(ValueError, match="no items"):
        m.review_plan("loop-1", approve=True, todolist="just prose, no items\n")

    assert "Implement the thing" in workspace.todolist.read_text(encoding="utf-8")


def test_reject_records_status_and_does_not_relaunch(
    workspace, registry, tmp_path, monkeypatch
):
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    m._upsert(_entry(workspace, tmp_path))

    started: list[dict] = []
    monkeypatch.setattr(m, "start", lambda **kw: started.append(kw))

    out = m.review_plan("loop-1", approve=False, note="T-001 太大")

    assert started == [], "reject must not spawn a process"
    assert out["status"] == "plan_rejected"
    assert plan_review.PlanReview.load(workspace).state == plan_review.REJECTED


def test_review_refuses_while_process_alive(
    workspace, registry, tmp_path, monkeypatch
):
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    m._upsert(_entry(workspace, tmp_path))
    monkeypatch.setattr(m, "_pid_matches", lambda pid, st: True)

    with pytest.raises(ValueError, match="still running"):
        m.review_plan("loop-1", approve=True)


def test_review_unknown_loop_returns_none(registry):
    assert m.review_plan("nope", approve=True) is None


def test_reject_never_overwrites_the_plan(
    workspace, registry, tmp_path, monkeypatch
):
    """The UI sends editor contents whenever edit mode is open; rejection must
    leave the persisted plan alone (we only promise edits are saved on approval).
    """
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    m._upsert(_entry(workspace, tmp_path))
    monkeypatch.setattr(m, "start", lambda **kw: {"status": "running"})

    edited = TODOLIST.replace("Implement the thing", "half-typed edit")
    m.review_plan("loop-1", approve=False, note="拆细", todolist=edited)

    assert "Implement the thing" in workspace.todolist.read_text(encoding="utf-8")


def test_consumed_gate_rejects_edit_before_writing(
    workspace, registry, tmp_path, monkeypatch
):
    """A stale approve against a consumed gate must fail *before* clobbering."""
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    plan_review.approve(workspace)
    plan_review.consume(workspace)
    m._upsert(_entry(workspace, tmp_path))
    monkeypatch.setattr(m, "start", lambda **kw: {"status": "running"})

    edited = TODOLIST.replace("Implement the thing", "stale editor contents")
    with pytest.raises(ValueError, match="consumed"):
        m.review_plan("loop-1", approve=True, todolist=edited)

    assert "Implement the thing" in workspace.todolist.read_text(encoding="utf-8")


def test_invalid_shape_edit_is_rejected_without_clobbering(
    workspace, registry, tmp_path, monkeypatch
):
    """Parseable but not a valid initial plan (all done) — still must not land."""
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    m._upsert(_entry(workspace, tmp_path))
    monkeypatch.setattr(m, "start", lambda **kw: {"status": "running"})

    all_done = TODOLIST.replace("status:pending", "status:done")
    with pytest.raises(ValueError, match="not a valid plan"):
        m.review_plan("loop-1", approve=True, todolist=all_done)

    assert "status:pending" in workspace.todolist.read_text(encoding="utf-8")


def test_stale_reject_against_consumed_gate_is_refused(
    workspace, registry, tmp_path, monkeypatch
):
    """A reject arriving after the loop finished must not present a completed
    run as plan_rejected (status derivation prioritizes the gate)."""
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    plan_review.approve(workspace)
    plan_review.consume(workspace)
    m._upsert(_entry(workspace, tmp_path))

    with pytest.raises(ValueError, match="consumed"):
        m.review_plan("loop-1", approve=False, note="too late")

    assert plan_review.PlanReview.load(workspace).state == plan_review.CONSUMED
    assert not plan_review.rejection_note_path(workspace).exists()
    state = {"cycle": 12, "last_decision": {"next": "done"}}
    assert m._derive_status_from_state(state, workspace) == "done"


def test_corrupt_gate_rejects_edit_before_writing(
    workspace, registry, tmp_path, monkeypatch
):
    plan_review.review_path(workspace).write_text('{"state": "appro', encoding="utf-8")
    m._upsert(_entry(workspace, tmp_path))
    monkeypatch.setattr(m, "start", lambda **kw: {"status": "running"})

    with pytest.raises(ValueError, match="unreadable"):
        m.review_plan("loop-1", approve=True, todolist="corrupt-driven overwrite\n")

    assert "Implement the thing" in workspace.todolist.read_text(encoding="utf-8")


def test_summary_exposes_plan_review(workspace, registry, tmp_path):
    from agentloop.todolist import parse

    plan_review.open_gate(workspace, parse(workspace))
    entry = _entry(workspace, tmp_path)
    m._upsert(entry)
    summary = m._summary(entry)
    assert summary["plan_review"]["state"] == "awaiting"
    assert summary["plan_review"]["stats"]["dev"] == 1
