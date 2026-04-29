"""Tests for agentloop/scheduler_writes.py + validator scheduler bypass."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import scheduler_writes as sw
from agentloop.todolist import (
    Attempt,
    Item,
    Todolist,
    parse as parse_todolist,
)
from agentloop.validator import ValidationError, validate_transition


# ---------- fixtures ---------------------------------------------------


def _dev(id_: str, status: str = "pending", deps=None) -> Item:
    return Item(
        id=id_, type="dev", status=status, title=f"dev {id_}",
        dependencies=list(deps or []),
    )


def _qa(id_: str, source: str, status: str = "pending", deps=None) -> Item:
    return Item(
        id=id_, type="qa", status=status, title=f"qa {id_}", source=source,
        dependencies=list(deps or []),
    )


def _tl(*items: Item) -> Todolist:
    return Todolist(metadata={"project": "test"}, items=list(items))


# ---------- validator scheduler bypass ---------------------------------


def test_validator_scheduler_bypass_allows_pending_to_abandoned():
    before = _tl(_dev("T-001", status="pending"))
    after = _tl(_dev("T-001", status="abandoned"))
    # Non-scheduler actors would reject this; scheduler is allowed to.
    validate_transition(before, after, "scheduler", None)


def test_validator_scheduler_bypass_allows_ready_for_qa_to_pending():
    before = _tl(_dev("T-001", status="ready_for_qa"))
    after = _tl(_dev("T-001", status="pending"))
    validate_transition(before, after, "scheduler", None)


def test_validator_still_rejects_invalid_shape_for_scheduler():
    before = _tl(_dev("T-001"))
    after = _tl(Item(id="T-001", type="dev", status="bogus_status", title="x"))
    with pytest.raises(ValidationError, match="invalid status"):
        validate_transition(before, after, "scheduler", None)


def test_validator_rejects_dev_writing_abandoned():
    before = _tl(_dev("T-001", status="pending"))
    after = _tl(_dev("T-001", status="abandoned"))
    # dev actor should NOT be allowed to write abandoned; that's scheduler-only.
    with pytest.raises(ValidationError, match="illegal status transition"):
        validate_transition(before, after, "dev", "T-001")


# ---------- mark_abandoned ---------------------------------------------


def test_mark_abandoned_appends_log_and_returns_true():
    tl = _tl(_dev("T-001", status="pending"))
    ok = sw.mark_abandoned(tl, "T-001", "exceeded max_item_attempts", cycle=7)
    assert ok is True
    it = tl.by_id("T-001")
    assert it.status == "abandoned"
    assert len(it.attempt_log) == 1
    assert it.attempt_log[0].cycle == 7
    assert "exceeded max_item_attempts" in it.attempt_log[0].notes


def test_mark_abandoned_noop_on_done_or_missing():
    tl = _tl(_dev("T-001", status="done"))
    assert sw.mark_abandoned(tl, "T-001", "x", cycle=1) is False
    assert sw.mark_abandoned(tl, "T-999", "x", cycle=1) is False


# ---------- downgrade_reviewed_dev -------------------------------------


def test_downgrade_reviewed_dev_ready_to_pending():
    tl = _tl(_dev("T-001", status="ready_for_qa"))
    ok = sw.downgrade_reviewed_dev(tl, "T-001", cycle=3, reason="qa abandoned")
    assert ok is True
    assert tl.by_id("T-001").status == "pending"
    assert "downgraded" in tl.by_id("T-001").attempt_log[-1].notes


def test_downgrade_reviewed_dev_noop_unless_ready():
    tl = _tl(_dev("T-001", status="pending"))
    assert sw.downgrade_reviewed_dev(tl, "T-001", cycle=3, reason="x") is False


# ---------- append_scheduler_attempt idempotency -----------------------


def test_append_scheduler_attempt_idempotent():
    it = _dev("T-001")
    assert sw.append_scheduler_attempt(it, 3, "pending", "reconcile: stalled") is True
    assert sw.append_scheduler_attempt(it, 3, "pending", "reconcile: stalled") is False
    # different cycle still appended
    assert sw.append_scheduler_attempt(it, 4, "pending", "reconcile: stalled") is True
    assert len(it.attempt_log) == 2


# ---------- create_dynamic_qa ------------------------------------------


def test_create_dynamic_qa_shape():
    tl = _tl(_dev("T-001", status="ready_for_qa"), _dev("T-002"))
    qa = sw.create_dynamic_qa(tl, dev_id="T-001", cycle=5)
    assert qa.type == "qa"
    assert qa.status == "pending"
    assert qa.source == "follows T-001"
    assert qa.dependencies == ["T-001"]
    # next_id picks T-003 since T-002 exists
    assert qa.id == "T-003"
    assert qa.attempt_log[0].notes.startswith("auto-created by scheduler")
    # item appears in todolist
    assert tl.by_id("T-003") is qa


# ---------- stale_doing_reconcile --------------------------------------


def test_stale_doing_reset_on_boot():
    tl = _tl(
        _dev("T-001", status="doing"),
        _dev("T-002", status="pending"),
        _dev("T-003", status="doing"),
    )
    reset = sw.stale_doing_reconcile(tl, boot_cycle=0)
    assert sorted(reset) == ["T-001", "T-003"]
    assert tl.by_id("T-001").status == "pending"
    assert tl.by_id("T-003").status == "pending"
    assert tl.by_id("T-002").status == "pending"
    # attempt_log carries note
    assert "stale doing reset" in tl.by_id("T-001").attempt_log[-1].notes


def test_stale_doing_reconcile_noop_when_none():
    tl = _tl(_dev("T-001"), _dev("T-002", status="done"))
    assert sw.stale_doing_reconcile(tl, boot_cycle=0) == []


# ---------- find_dep_cycles --------------------------------------------


def test_dep_cycle_detected_two_node():
    tl = _tl(
        _dev("T-001", deps=["T-002"]),
        _dev("T-002", deps=["T-001"]),
    )
    cycles = sw.find_dep_cycles(tl)
    assert len(cycles) == 1
    assert sorted(cycles[0]) == ["T-001", "T-002"]


def test_dep_cycle_detected_self_loop():
    tl = _tl(_dev("T-001", deps=["T-001"]))
    cycles = sw.find_dep_cycles(tl)
    assert cycles == [["T-001"]]


def test_dep_cycle_none_for_dag():
    tl = _tl(
        _dev("T-001"),
        _dev("T-002", deps=["T-001"]),
        _dev("T-003", deps=["T-001", "T-002"]),
    )
    assert sw.find_dep_cycles(tl) == []


# ---------- find_dangling_deps -----------------------------------------


def test_dangling_deps_detected():
    tl = _tl(
        _dev("T-001"),
        _dev("T-002", deps=["T-001", "T-999"]),
    )
    dangling = sw.find_dangling_deps(tl)
    assert dangling == [("T-002", "T-999")]


def test_no_dangling_when_all_defined():
    tl = _tl(_dev("T-001"), _dev("T-002", deps=["T-001"]))
    assert sw.find_dangling_deps(tl) == []


# ---------- startup_health integration ---------------------------------


def test_startup_health_exhausts_on_dep_cycle(tmp_path: Path):
    from agentloop.loop import _startup_health
    from agentloop.state import LoopState
    from agentloop.todolist import write as write_todolist

    tl = _tl(
        _dev("T-001", deps=["T-002"]),
        _dev("T-002", deps=["T-001"]),
    )
    write_todolist(tmp_path, tl)

    state = LoopState()
    _startup_health(tmp_path, state)
    assert state.exhausted_reason is not None
    assert "dependency cycle" in state.exhausted_reason


def test_startup_health_exhausts_on_dangling_dep(tmp_path: Path):
    from agentloop.loop import _startup_health
    from agentloop.state import LoopState
    from agentloop.todolist import write as write_todolist

    tl = _tl(_dev("T-001", deps=["T-999"]))
    write_todolist(tmp_path, tl)

    state = LoopState()
    _startup_health(tmp_path, state)
    assert state.exhausted_reason is not None
    assert "dangling" in state.exhausted_reason


def test_startup_health_resets_stale_doing(tmp_path: Path):
    from agentloop.loop import _startup_health
    from agentloop.state import LoopState
    from agentloop.todolist import write as write_todolist

    tl = _tl(_dev("T-001", status="doing"), _dev("T-002"))
    write_todolist(tmp_path, tl)

    state = LoopState()
    _startup_health(tmp_path, state)
    assert state.exhausted_reason is None

    # Re-parse from disk — stale reset must persist
    reloaded = parse_todolist(tmp_path)
    assert reloaded.by_id("T-001").status == "pending"
    assert any(
        ev.get("kind") == "stale_doing_reconciled"
        for ev in state.scheduler_events
    )


def test_startup_health_noop_on_empty_todolist(tmp_path: Path):
    from agentloop.loop import _startup_health
    from agentloop.state import LoopState

    state = LoopState()
    # no todolist.md present
    _startup_health(tmp_path, state)
    assert state.exhausted_reason is None
