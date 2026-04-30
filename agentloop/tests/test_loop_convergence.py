"""Tests for loop-level fuse/cascade/classify_terminal helpers + PM abandoned awareness."""
from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import loop as scheduler
from agentloop.agents import pm as pm_agent
from agentloop.loop import ExitCode, _cascade, _classify_terminal, _fuse
from agentloop.state import Decision
from agentloop.todolist import Attempt, Item, Todolist
from agentloop.workspace import WorkspacePaths


def _ws(tmp_path: Path, slug: str = "test-ws") -> WorkspacePaths:
    return WorkspacePaths.for_workspace(tmp_path, slug)


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
    newly, downgraded = _cascade(tmp_path, tl, cycle=7)
    ids = {i for i, _ in newly}
    assert ids == {"T-002", "T-003"}
    assert downgraded == []
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
    newly, downgraded = _cascade(tmp_path, tl, cycle=7)
    # dev should be rolled back to pending so fuse gets another shot
    assert tl.by_id("T-001").status == "pending"
    assert "downgraded" in tl.by_id("T-001").attempt_log[-1].notes
    # regression: downgrade must be reported so the loop persists it
    assert newly == []
    assert [i for i, _ in downgraded] == ["T-001"]


def test_cascade_aggregated_qa_downgrades_all_reviewed_devs(tmp_path: Path):
    """Aggregated qa abandoned → every reviewed dev at ready_for_qa rolls back."""
    tl = _tl(
        _dev("T-001", status="ready_for_qa"),
        _dev("T-002", status="ready_for_qa"),
        _dev("T-003", status="ready_for_qa"),
        _qa("T-010", source="follows T-001, T-002, T-003", status="abandoned"),
    )
    _, downgraded = _cascade(tmp_path, tl, cycle=7)
    assert {i for i, _ in downgraded} == {"T-001", "T-002", "T-003"}
    for dev_id in ("T-001", "T-002", "T-003"):
        assert tl.by_id(dev_id).status == "pending", dev_id
        assert "downgraded" in tl.by_id(dev_id).attempt_log[-1].notes


def test_cascade_empty_when_nothing_abandoned(tmp_path: Path):
    tl = _tl(_dev("T-001"), _dev("T-002", deps=["T-001"]))
    assert _cascade(tmp_path, tl, cycle=3) == ([], [])


def test_loop_persists_downgrade_only_cascade(tmp_path: Path, monkeypatch):
    """Regression (Codex P1): when _cascade only downgrades (no newly
    abandoned) the main loop must still call scheduler_write, otherwise PM
    dispatches a dev that the agent then sees as ``ready_for_qa`` on disk.
    """
    from agentloop import loop as scheduler
    from agentloop.agents.base import RunResult
    from agentloop.todolist import TODOLIST_FILE, parse as parse_tl, write as write_tl

    ws = _ws(tmp_path)
    tl = _tl(
        _dev("T-001", status="ready_for_qa"),
        _qa("T-002", source="follows T-001", status="abandoned"),
    )
    write_tl(ws, tl)
    design_path = tmp_path / "design.md"
    design_path.write_text("# design\n", encoding="utf-8")

    observed_status: dict[str, str] = {}

    def spy_dev(cwd: Path, item_id: str, cycle: int, cfg) -> RunResult:
        # What the agent would see on disk at dispatch time.
        on_disk = parse_tl(cwd).by_id(item_id)
        observed_status[item_id] = on_disk.status if on_disk else "missing"
        # Finish the item so the loop can exit.
        on_disk.status = "ready_for_qa"
        from agentloop.todolist import Attempt, write as wt
        on_disk.attempt_log.append(Attempt(cycle, "ready_for_qa", "ok"))
        tl2 = parse_tl(cwd)
        target = tl2.by_id(item_id)
        target.status = "ready_for_qa"
        target.attempt_log.append(Attempt(cycle, "ready_for_qa", "ok"))
        wt(cwd, tl2)
        return RunResult(stream_json_path=Path("/dev/null"), duration_sec=0.1,
                         cost_cny=0.0, success=True, errors=[])

    def spy_qa(cwd: Path, item_id: str, cycle: int, cfg) -> RunResult:
        tl2 = parse_tl(cwd)
        qa = tl2.by_id(item_id)
        from agentloop.validator import _reviewed_dev_ids
        for dev_id in _reviewed_dev_ids(qa, tl2):
            d = tl2.by_id(dev_id)
            if d is not None:
                d.status = "done"
        qa.status = "done"
        from agentloop.todolist import write as wt
        wt(cwd, tl2)
        return RunResult(stream_json_path=Path("/dev/null"), duration_sec=0.1,
                         cost_cny=0.0, success=True, errors=[])

    monkeypatch.setattr("agentloop.loop.dev_agent.run", spy_dev)
    monkeypatch.setattr("agentloop.loop.qa_agent.run", spy_qa)

    scheduler.run(design_path, max_cycles=10, ws=ws)
    # The dev agent must have observed T-001 at ``pending``, never the
    # stale ``ready_for_qa`` that would leak through an unpersisted cascade.
    assert observed_status.get("T-001") == "pending", observed_status


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


# ---------- decision_advanced + reconcile + fingerprint --------------


def test_decision_advanced_dev_ready_for_qa():
    from agentloop.loop import _decision_advanced
    before = _tl(_dev("T-001", status="pending"))
    after = _tl(
        Item(id="T-001", type="dev", status="ready_for_qa", title="dev T-001",
             attempt_log=[Attempt(1, "ready_for_qa", "impl done")])
    )
    d = Decision(next="dev", item_id="T-001", reason="x")
    assert _decision_advanced(before, after, d) is True


def test_decision_advanced_dev_attempt_log_growth():
    from agentloop.loop import _decision_advanced
    before = _tl(_dev("T-001", status="pending"))
    after = _tl(
        Item(id="T-001", type="dev", status="pending", title="dev T-001",
             attempt_log=[Attempt(1, "pending", "first fail")])
    )
    d = Decision(next="dev", item_id="T-001", reason="x")
    assert _decision_advanced(before, after, d) is True


def test_decision_not_advanced_silent_failure():
    from agentloop.loop import _decision_advanced
    before = _tl(_dev("T-001", status="pending"))
    after = _tl(_dev("T-001", status="pending"))  # identical
    d = Decision(next="dev", item_id="T-001", reason="x")
    assert _decision_advanced(before, after, d) is False


def test_decision_advanced_qa_done():
    from agentloop.loop import _decision_advanced
    before = _tl(
        _dev("T-001", status="ready_for_qa"),
        _qa("T-002", source="follows T-001", status="pending"),
    )
    after = _tl(
        _dev("T-001", status="done"),
        _qa("T-002", source="follows T-001", status="done"),
    )
    d = Decision(next="qa", item_id="T-002", reason="x")
    assert _decision_advanced(before, after, d) is True


def test_decision_advanced_aggregated_qa_any_reviewed_dev_moves():
    """_decision_advanced must scan all reviewed devs, not just the first.

    Regression: aggregated qa (source "follows T-001, T-002") that moved only
    T-002 previously reported False because the helper only inspected T-001.
    """
    from agentloop.loop import _decision_advanced

    before = _tl(
        _dev("T-001", status="ready_for_qa"),
        _dev("T-002", status="ready_for_qa"),
        _qa("T-010", source="follows T-001, T-002", status="pending"),
    )
    # qa stayed pending and didn't touch its own attempt_log, but T-002 moved
    after = _tl(
        _dev("T-001", status="ready_for_qa"),
        _dev("T-002", status="done"),
        _qa("T-010", source="follows T-001, T-002", status="pending"),
    )
    d = Decision(next="qa", item_id="T-010", reason="x")
    assert _decision_advanced(before, after, d) is True


def test_reconcile_idempotent_digest(tmp_path: Path):
    from agentloop.loop import _reconcile
    from agentloop.agents.base import RunResult
    from agentloop.todolist import write as write_todolist

    tl = _tl(_dev("T-001", status="doing"))
    ws = _ws(tmp_path)
    write_todolist(ws, tl)
    decision = Decision(next="dev", item_id="T-001", reason="x")
    result = RunResult(
        stream_json_path=Path("/dev/null"), duration_sec=0.0,
        cost_cny=0.0, success=False, errors=["agent stalled after 180s idle"],
    )

    _reconcile(ws, tl, decision, result, cycle=3)
    assert tl.by_id("T-001").status == "pending"
    assert len(tl.by_id("T-001").attempt_log) == 1
    assert "stalled" in tl.by_id("T-001").attempt_log[-1].notes

    # same cycle + same notes → no-op
    _reconcile(ws, tl, decision, result, cycle=3)
    assert len(tl.by_id("T-001").attempt_log) == 1

    # next cycle → appended
    _reconcile(ws, tl, decision, result, cycle=4)
    assert len(tl.by_id("T-001").attempt_log) == 2


def test_classify_run_failure_variants():
    from agentloop.agents.base import RunResult
    from agentloop.loop import _classify_run_failure

    stalled = RunResult(
        stream_json_path=Path("/dev/null"), duration_sec=0.0, cost_cny=0.0,
        success=False, errors=["agent Stalled (boundary bug)"],
    )
    assert "stall" in _classify_run_failure(stalled).lower()

    exited = RunResult(
        stream_json_path=Path("/dev/null"), duration_sec=0.0, cost_cny=0.0,
        success=False, errors=["claude-cli exit code 1"],
    )
    assert "exit" in _classify_run_failure(exited).lower() or "error" in _classify_run_failure(exited).lower()

    silent = RunResult(
        stream_json_path=Path("/dev/null"), duration_sec=0.0, cost_cny=0.0,
        success=True, errors=[],
    )
    assert "clean" in _classify_run_failure(silent).lower()


# ---------- fingerprint ------------------------------------------------


def test_fingerprint_changes_on_status_change():
    from agentloop.loop import _fingerprint
    a = _tl(_dev("T-001", status="pending"))
    b = _tl(_dev("T-001", status="ready_for_qa"))
    assert _fingerprint(a) != _fingerprint(b)


def test_fingerprint_changes_on_attempt_log_growth():
    from agentloop.loop import _fingerprint
    a = _tl(_dev("T-001", status="pending"))
    b = _tl(Item(id="T-001", type="dev", status="pending", title="dev T-001",
                 attempt_log=[Attempt(1, "pending", "x")]))
    assert _fingerprint(a) != _fingerprint(b)


def test_fingerprint_stuck_requires_threshold_identical():
    from agentloop.loop import _fingerprint_stuck
    assert _fingerprint_stuck(["a", "a", "a"], threshold=4) is False  # need 4
    assert _fingerprint_stuck(["a", "a", "a", "a"], threshold=4) is True
    assert _fingerprint_stuck(["a", "a", "b", "a"], threshold=4) is False


# ---------- extract_dev_id_from_reason --------------------------------


def test_extract_dev_id_from_reason():
    from agentloop.loop import _extract_dev_id_from_reason
    assert _extract_dev_id_from_reason("ready_for_qa T-007 has no matching qa item") == "T-007"
    assert _extract_dev_id_from_reason("nothing here") is None
    assert _extract_dev_id_from_reason("") is None


# ---------- dynamic qa creation path ----------------------------------


def test_orphan_dev_auto_qa_created(tmp_path: Path, monkeypatch):
    """End-to-end: ready_for_qa dev without a qa item → loop creates one and
    dispatches the qa agent on the next decision."""
    from agentloop import loop as scheduler
    from agentloop.agents.base import RunResult

    tl = _tl(_dev("T-001", status="ready_for_qa"))
    from agentloop.todolist import write as write_todolist
    ws = _ws(tmp_path)
    write_todolist(ws, tl)

    # Build a design file so loop.run proceeds past planner phase.
    design_path = tmp_path / "design.md"
    design_path.write_text("# design\n", encoding="utf-8")

    calls = {"qa": 0, "dev": 0}

    def fake_qa_run(cwd, item_id: str, cycle: int, cfg) -> RunResult:
        # QA marks the reviewed dev done + itself done
        from agentloop.todolist import parse as parse_tl, write as write_tl
        tl = parse_tl(cwd)
        dev = tl.by_id("T-001")
        qa = tl.by_id(item_id)
        dev.status = "done"
        qa.status = "done"
        write_tl(cwd, tl)
        calls["qa"] += 1
        return RunResult(stream_json_path=Path("/dev/null"), duration_sec=0.1, cost_cny=0.0, success=True, errors=[])

    def fake_dev_run(*args, **kwargs) -> RunResult:
        calls["dev"] += 1
        return RunResult(stream_json_path=Path("/dev/null"), duration_sec=0.1, cost_cny=0.0, success=True, errors=[])

    monkeypatch.setattr("agentloop.loop.qa_agent.run", fake_qa_run)
    monkeypatch.setattr("agentloop.loop.dev_agent.run", fake_dev_run)

    result = scheduler.run(design_path, max_cycles=5, ws=ws)
    assert result.code == scheduler.ExitCode.SUCCESS, result.reason
    assert calls["qa"] == 1
    # After QA pass: T-001 done + dynamic T-002 done → 2 items total
    from agentloop.todolist import parse as parse_tl
    tl2 = parse_tl(ws)
    assert len(tl2.items) == 2
    assert tl2.by_id("T-002") is not None  # dynamic qa
    assert tl2.by_id("T-002").status == "done"
    assert tl2.by_id("T-002").source == "follows T-001"


# ---------- per-item failure does not kill loop -----------------------


def test_per_item_failure_does_not_kill_loop(tmp_path: Path, monkeypatch):
    """A failed item should fuse → abandoned; independent items still complete;
    final result = PARTIAL_SUCCESS."""
    from agentloop import loop as scheduler
    from agentloop.agents.base import RunResult
    from agentloop.todolist import write as write_todolist

    tl = _tl(
        _dev("T-001"),            # will fail 5x and get fused
        _dev("T-010"),            # independent: will pass
    )
    ws = _ws(tmp_path)
    write_todolist(ws, tl)
    design_path = tmp_path / "design.md"
    design_path.write_text("# design\n", encoding="utf-8")

    def fake_dev_run(cwd, item_id: str, cycle: int, cfg) -> RunResult:
        from agentloop.todolist import parse as parse_tl, write as write_tl, Attempt
        tl = parse_tl(cwd)
        item = tl.by_id(item_id)
        if item_id == "T-001":
            # always "fail": append a pending attempt, keep status pending
            item.attempt_log.append(Attempt(cycle, "pending", "flaky"))
        else:
            item.status = "ready_for_qa"
            item.attempt_log.append(Attempt(cycle, "ready_for_qa", "done"))
        write_tl(cwd, tl)
        return RunResult(stream_json_path=Path("/dev/null"), duration_sec=0.1, cost_cny=0.0, success=True, errors=[])

    def fake_qa_run(cwd, item_id: str, cycle: int, cfg) -> RunResult:
        from agentloop.todolist import parse as parse_tl, write as write_tl
        tl = parse_tl(cwd)
        qa = tl.by_id(item_id)
        # qa targets the dev(s) referenced in source; mark all reviewed devs
        # + itself done.
        from agentloop.validator import _reviewed_dev_ids
        for dev_id in _reviewed_dev_ids(qa, tl):
            dev = tl.by_id(dev_id)
            if dev is not None:
                dev.status = "done"
        qa.status = "done"
        write_tl(cwd, tl)
        return RunResult(stream_json_path=Path("/dev/null"), duration_sec=0.1, cost_cny=0.0, success=True, errors=[])

    monkeypatch.setattr("agentloop.loop.dev_agent.run", fake_dev_run)
    monkeypatch.setattr("agentloop.loop.qa_agent.run", fake_qa_run)

    result = scheduler.run(design_path, max_cycles=30, ws=ws)
    assert result.code == scheduler.ExitCode.PARTIAL_SUCCESS, result.reason

    from agentloop.todolist import parse as parse_tl
    tl2 = parse_tl(ws)
    assert tl2.by_id("T-001").status == "abandoned"
    assert tl2.by_id("T-010").status == "done"


# ---------- fingerprint stuck exhausts ---------------------------------
# Note: the integration-level fingerprint-stuck scenario is hard to construct
# organically — any silently-failing dispatch triggers ``_reconcile`` which
# appends a new attempt, growing the attempt_log and changing the fingerprint.
# The unit tests above (``test_fingerprint_stuck_requires_threshold_identical``
# and friends) cover the helper logic. The scheduler's stuck-exit branch is
# exercised implicitly by the planner_retry and cycle tests below.


# ---------- planner retry exhausts -------------------------------------


def test_planner_retry_exhausts(tmp_path: Path, monkeypatch):
    """Planner that always fails must retry N times then exit ERROR."""
    from agentloop import loop as scheduler
    from agentloop.agents.base import RunResult

    design_path = tmp_path / "design.md"
    design_path.write_text("# design\n", encoding="utf-8")
    ws = _ws(tmp_path)

    calls = {"n": 0}

    def failing_planner(ws_arg, cfg, design_arg) -> RunResult:
        calls["n"] += 1
        # Leave no todolist file (simulate total failure).
        return RunResult(stream_json_path=Path("/dev/null"), duration_sec=0.1, cost_cny=0.0, success=False, errors=["boom"])

    monkeypatch.setattr("agentloop.loop.planner_agent.run", failing_planner)

    from agentloop.config import AgentConfig
    real_load = AgentConfig.load

    def patched_load(cwd):
        c = real_load(cwd)
        c.limits.max_planner_attempts = 3
        return c

    monkeypatch.setattr("agentloop.loop.AgentConfig.load", patched_load)
    result = scheduler.run(design_path, ws=ws)
    assert result.code == scheduler.ExitCode.ERROR
    assert calls["n"] == 3


def test_dep_cycle_exhausts_immediately(tmp_path: Path):
    """Phase-1 entry dep-cycle detection → EXHAUSTED before any dispatch."""
    from agentloop import loop as scheduler
    from agentloop.todolist import write as write_todolist

    tl = _tl(
        _dev("T-001", deps=["T-002"]),
        _dev("T-002", deps=["T-001"]),
    )
    ws = _ws(tmp_path)
    write_todolist(ws, tl)
    design_path = tmp_path / "design.md"
    design_path.write_text("# design\n", encoding="utf-8")

    result = scheduler.run(design_path, ws=ws)
    assert result.code == scheduler.ExitCode.EXHAUSTED
    assert "cycle" in result.reason


def test_stall_killed_doing_recovered(tmp_path: Path, monkeypatch):
    """A dev left in ``doing`` (simulating stall-kill of prior run) must be
    reset on startup_health and the loop must reschedule it."""
    from agentloop import loop as scheduler
    from agentloop.agents.base import RunResult
    from agentloop.todolist import write as write_todolist, parse as parse_tl

    tl = _tl(_dev("T-001", status="doing"))
    ws = _ws(tmp_path)
    write_todolist(ws, tl)
    design_path = tmp_path / "design.md"
    design_path.write_text("# design\n", encoding="utf-8")

    def fake_dev_run(cwd, item_id: str, cycle: int, cfg) -> RunResult:
        from agentloop.todolist import Attempt
        tl = parse_tl(cwd)
        item = tl.by_id(item_id)
        item.status = "ready_for_qa"
        item.attempt_log.append(Attempt(cycle, "ready_for_qa", "impl done"))
        from agentloop.todolist import write as wt
        wt(cwd, tl)
        return RunResult(stream_json_path=Path("/dev/null"), duration_sec=0.1, cost_cny=0.0, success=True, errors=[])

    def fake_qa_run(cwd, item_id: str, cycle: int, cfg) -> RunResult:
        tl = parse_tl(cwd)
        qa = tl.by_id(item_id)
        from agentloop.validator import _reviewed_dev_ids
        for dev_id in _reviewed_dev_ids(qa, tl):
            dev = tl.by_id(dev_id)
            if dev is not None:
                dev.status = "done"
        qa.status = "done"
        from agentloop.todolist import write as wt
        wt(cwd, tl)
        return RunResult(stream_json_path=Path("/dev/null"), duration_sec=0.1, cost_cny=0.0, success=True, errors=[])

    monkeypatch.setattr("agentloop.loop.dev_agent.run", fake_dev_run)
    monkeypatch.setattr("agentloop.loop.qa_agent.run", fake_qa_run)

    result = scheduler.run(design_path, max_cycles=10, ws=ws)
    assert result.code == scheduler.ExitCode.SUCCESS
    tl2 = parse_tl(ws)
    assert tl2.by_id("T-001").status == "done"
