"""Tests for loop-level fuse/cascade/classify_terminal helpers + PM abandoned awareness."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import loop as scheduler
from agentloop.agents import pm as pm_agent
from agentloop.loop import ExitCode, _cascade, _classify_terminal, _fuse
from agentloop.state import Decision
from agentloop.todolist import Attempt, Item, Todolist


# ---------- fixtures ---------------------------------------------------


def _dev(id_: str, status: str = "pending", deps=None, failures: int = 0) -> Item:
    log = [Attempt(cycle=i + 1, result="pending", notes="fail") for i in range(failures)]
    return Item(
        id=id_, type="dev", status=status, title=f"dev {id_}",
        dependencies=list(deps or []), attempt_log=log,
    )


def _qa(id_: str, source: str, status: str = "pending", deps=None) -> Item:
    return Item(
        id=id_, type="qa", status=status, title=f"qa {id_}", source=source,
        dependencies=list(deps or []),
    )


def _tl(*items: Item) -> Todolist:
    return Todolist(metadata={"project": "test"}, items=list(items))


# ---------- fuse -------------------------------------------------------


def test_fuse_marks_over_limit_abandoned(tmp_path: Path):
    tl = _tl(_dev("T-001", failures=5), _dev("T-002", failures=2))
    newly = _fuse(tmp_path, tl, max_attempts=5, cycle=6)
    assert [(i, "exceeded" in r) for i, r in newly] == [("T-001", True)]
    assert tl.by_id("T-001").status == "abandoned"
    assert tl.by_id("T-002").status == "pending"


def test_fuse_idempotent_on_terminal(tmp_path: Path):
    tl = _tl(_dev("T-001", status="abandoned", failures=5))
    newly = _fuse(tmp_path, tl, max_attempts=5, cycle=7)
    assert newly == []
    assert tl.by_id("T-001").status == "abandoned"


def test_fuse_respects_done(tmp_path: Path):
    tl = _tl(_dev("T-001", status="done", failures=10))
    newly = _fuse(tmp_path, tl, max_attempts=5, cycle=7)
    assert newly == []
    assert tl.by_id("T-001").status == "done"


# ---------- cascade ----------------------------------------------------


def test_cascade_closure_single_chain(tmp_path: Path):
    tl = _tl(
        _dev("T-001", status="abandoned"),
        _dev("T-002", deps=["T-001"]),
        _dev("T-003", deps=["T-002"]),
    )
    newly = _cascade(tmp_path, tl, cycle=7)
    ids = {i for i, _ in newly}
    assert ids == {"T-002", "T-003"}
    assert tl.by_id("T-002").status == "abandoned"
    assert tl.by_id("T-003").status == "abandoned"


def test_cascade_independent_branch_survives(tmp_path: Path):
    tl = _tl(
        _dev("T-001", status="abandoned"),
        _dev("T-002", deps=["T-001"]),        # should cascade
        _dev("T-010"),                          # independent branch
        _dev("T-011", deps=["T-010"]),
    )
    _cascade(tmp_path, tl, cycle=7)
    assert tl.by_id("T-002").status == "abandoned"
    assert tl.by_id("T-010").status == "pending"
    assert tl.by_id("T-011").status == "pending"


def test_cascade_qa_abandoned_downgrades_dev(tmp_path: Path):
    tl = _tl(
        _dev("T-001", status="ready_for_qa"),
        _qa("T-002", source="follows T-001", status="abandoned"),
    )
    _cascade(tmp_path, tl, cycle=7)
    # dev should be rolled back to pending so fuse gets another shot
    assert tl.by_id("T-001").status == "pending"
    assert "downgraded" in tl.by_id("T-001").attempt_log[-1].notes


def test_cascade_empty_when_nothing_abandoned(tmp_path: Path):
    tl = _tl(_dev("T-001"), _dev("T-002", deps=["T-001"]))
    assert _cascade(tmp_path, tl, cycle=3) == []


# ---------- classify_terminal -----------------------------------------


def test_classify_terminal_all_done():
    tl = _tl(_dev("T-001", status="done"), _qa("T-002", "follows T-001", status="done"))
    code, _ = _classify_terminal(tl, Decision(next="done", item_id=None, reason="ok"))
    assert code == ExitCode.SUCCESS


def test_classify_terminal_partial_success():
    tl = _tl(
        _dev("T-001", status="done"),
        _dev("T-002", status="abandoned"),
    )
    code, reason = _classify_terminal(tl, Decision(next="done", item_id=None, reason="x"))
    assert code == ExitCode.PARTIAL_SUCCESS
    assert "1 item" in reason


def test_classify_terminal_exhausted_when_items_live():
    tl = _tl(_dev("T-001", status="done"), _dev("T-002", status="pending"))
    code, _ = _classify_terminal(tl, Decision(next="done", item_id=None, reason="stuck"))
    assert code == ExitCode.EXHAUSTED


def test_classify_terminal_empty():
    tl = _tl()
    code, _ = _classify_terminal(tl, Decision(next="done", item_id=None, reason="x"))
    assert code == ExitCode.SUCCESS


# ---------- PM abandoned awareness ------------------------------------


def test_pm_skips_abandoned_when_scheduling_dev():
    tl = _tl(
        _dev("T-001", status="abandoned"),
        _dev("T-002", status="pending", deps=["T-001"]),
    )
    # abandoned dep allows downstream to schedule (abandon is terminal)
    d = pm_agent.decide(tl)
    assert d.next == "dev"
    assert d.item_id == "T-002"


def test_pm_all_done_with_abandoned_returns_done():
    tl = _tl(
        _dev("T-001", status="done"),
        _dev("T-002", status="abandoned"),
    )
    d = pm_agent.decide(tl)
    assert d.next == "done"
    assert "abandoned" in d.reason


def test_pm_does_not_select_abandoned_qa():
    tl = _tl(
        _dev("T-001", status="ready_for_qa"),
        _qa("T-002", source="follows T-001", status="abandoned"),
    )
    d = pm_agent.decide(tl)
    # qa is abandoned, so treat as "no matching qa" → dynamic qa flow
    assert d.next == "qa"
    assert d.item_id is None


def test_pm_all_abandoned_is_partial_done():
    tl = _tl(
        _dev("T-001", status="abandoned"),
        _dev("T-002", status="abandoned"),
    )
    d = pm_agent.decide(tl)
    assert d.next == "done"
    assert "abandoned" in d.reason
