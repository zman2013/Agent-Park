"""Tests for the state machine: validator + PM + todolist parsing round-trip."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop.agents import pm as pm_agent
from agentloop.state import Decision, Limits, LoopState
from agentloop.todolist import (
    Attempt,
    Item,
    Todolist,
    parse_text,
    render,
    trim_attempt_log,
)
from agentloop.validator import ValidationError, validate_transition


# ---------- fixtures ----------------------------------------------------


def _dev(id_: str, status: str = "pending", **kw) -> Item:
    return Item(id=id_, type="dev", status=status, title=f"dev {id_}", **kw)


def _qa(id_: str, source: str, status: str = "pending", **kw) -> Item:
    return Item(id=id_, type="qa", status=status, title=f"qa {id_}", source=source, **kw)


def _tl(*items: Item) -> Todolist:
    return Todolist(metadata={"project": "test"}, items=list(items))


# ---------- todolist round-trip ----------------------------------------


def test_todolist_round_trip():
    original = _tl(
        _dev("T-001", status="done", dev_notes="骨架 OK"),
        _qa("T-002", source="follows T-001", status="done"),
        Item(
            id="T-003",
            type="dev",
            status="pending",
            title="实现校验",
            dependencies=["T-001"],
            attempt_log=[
                Attempt(cycle=2, result="pending", notes="qa findings: 缺少空串校验"),
                Attempt(cycle=5, result="ready_for_qa", notes="dev_notes: 修正了"),
            ],
        ),
    )
    text = render(original)
    parsed = parse_text(text)
    assert len(parsed.items) == 3

    roundtripped_ids = [it.id for it in parsed.items]
    assert roundtripped_ids == ["T-001", "T-002", "T-003"]

    t3 = parsed.by_id("T-003")
    assert t3 is not None
    assert t3.dependencies == ["T-001"]
    assert len(t3.attempt_log) == 2
    assert t3.attempt_log[0].cycle == 2
    assert t3.attempt_log[0].result == "pending"
    assert "缺少空串校验" in t3.attempt_log[0].notes


def test_next_id():
    tl = _tl(_dev("T-001"), _dev("T-007"))
    assert tl.next_id() == "T-008"


def test_abandoned_status_roundtrip():
    original = _tl(
        _dev("T-001", status="abandoned",
             attempt_log=[Attempt(1, "pending", "qa findings: exceeded limit")]),
        _qa("T-002", source="follows T-001", status="abandoned"),
    )
    text = render(original)
    parsed = parse_text(text)

    t1 = parsed.by_id("T-001")
    assert t1 is not None
    assert t1.status == "abandoned"
    assert len(t1.attempt_log) == 1
    assert t1.attempt_log[0].result == "pending"

    t2 = parsed.by_id("T-002")
    assert t2 is not None
    assert t2.status == "abandoned"


def test_trim_attempt_log_keeps_first_and_last():
    log = [
        Attempt(1, "pending", "first"),
        Attempt(3, "pending", "middle"),
        Attempt(5, "pending", "recent"),
        Attempt(7, "pending", "latest"),
    ]
    trimmed = trim_attempt_log(log, max_keep=3)
    assert [a.notes for a in trimmed] == ["first", "recent", "latest"]


# ---------- validator: planner ------------------------------------------


def test_planner_needs_empty_before():
    before = _tl(_dev("T-001"))
    after = _tl(_dev("T-001"), _dev("T-002"))
    with pytest.raises(ValidationError, match="already has items"):
        validate_transition(before, after, "planner", None)


def test_planner_needs_at_least_one_item():
    before = _tl()
    after = _tl()
    with pytest.raises(ValidationError, match="at least one"):
        validate_transition(before, after, "planner", None)


def test_planner_cannot_emit_done():
    before = _tl()
    after = _tl(_dev("T-001", status="done"))
    with pytest.raises(ValidationError, match="'done'"):
        validate_transition(before, after, "planner", None)


def test_planner_valid():
    before = _tl()
    after = _tl(_dev("T-001"), _qa("T-002", "follows T-001"))
    validate_transition(before, after, "planner", None)  # should not raise


# ---------- validator: dev ----------------------------------------------


def test_dev_illegal_transition():
    before = _tl(_dev("T-001", status="done"))
    after = _tl(_dev("T-001", status="pending"))  # reverting done is forbidden
    with pytest.raises(ValidationError, match="done"):
        validate_transition(before, after, "dev", "T-001")


def test_dev_cannot_touch_other_item():
    before = _tl(_dev("T-001"), _dev("T-003"))
    after = _tl(
        _dev("T-001", status="doing"),
        _dev("T-003", status="doing"),   # not the assigned item
    )
    with pytest.raises(ValidationError, match="was assigned"):
        validate_transition(before, after, "dev", "T-001")


def test_dev_cannot_add_items():
    before = _tl(_dev("T-001"))
    after = _tl(_dev("T-001"), _qa("T-002", "follows T-001"))
    with pytest.raises(ValidationError, match="cannot add"):
        validate_transition(before, after, "dev", "T-001")


def test_dev_pending_to_ready_for_qa():
    before = _tl(_dev("T-001"))
    after = _tl(_dev("T-001", status="ready_for_qa",
                     attempt_log=[Attempt(1, "ready_for_qa", "impl done")]))
    # 文件契约下 dev 一次写入即完成，允许 pending → ready_for_qa 直跳。
    validate_transition(before, after, "dev", "T-001")


def test_dev_doing_to_ready_for_qa_ok():
    before = _tl(_dev("T-001", status="doing"))
    after = _tl(_dev("T-001", status="ready_for_qa",
                     attempt_log=[Attempt(1, "ready_for_qa", "impl done")]))
    validate_transition(before, after, "dev", "T-001")


# ---------- validator: qa -----------------------------------------------


def test_qa_pass_marks_dev_done():
    before = _tl(
        _dev("T-001", status="ready_for_qa"),
        _qa("T-002", source="follows T-001"),
    )
    after = _tl(
        _dev("T-001", status="done"),
        _qa("T-002", source="follows T-001", status="done", findings="无"),
    )
    validate_transition(before, after, "qa", "T-002")


def test_qa_fail_sends_dev_back_and_appends_fix():
    before = _tl(
        _dev("T-001", status="ready_for_qa"),
        _qa("T-002", source="follows T-001"),
    )
    after = _tl(
        _dev("T-001", status="pending",
             attempt_log=[Attempt(3, "pending", "qa findings: X → T-003")]),
        _qa("T-002", source="follows T-001", status="done",
            findings="缺少 X"),
        _dev("T-003", status="pending"),
        _qa("T-004", source="follows T-003"),
    )
    validate_transition(before, after, "qa", "T-002")


def test_qa_cannot_modify_unrelated_item():
    before = _tl(
        _dev("T-001", status="ready_for_qa"),
        _qa("T-002", source="follows T-001"),
        _dev("T-003", status="pending"),  # unrelated
    )
    after = _tl(
        _dev("T-001", status="done"),
        _qa("T-002", source="follows T-001", status="done"),
        _dev("T-003", status="doing"),    # qa touched T-003!
    )
    with pytest.raises(ValidationError, match="unrelated"):
        validate_transition(before, after, "qa", "T-002")


def test_qa_cannot_touch_done_item():
    before = _tl(
        _dev("T-000", status="done"),
        _dev("T-001", status="ready_for_qa"),
        _qa("T-002", source="follows T-001"),
    )
    after = _tl(
        _dev("T-000", status="pending"),  # illegal
        _dev("T-001", status="done"),
        _qa("T-002", source="follows T-001", status="done"),
    )
    with pytest.raises(ValidationError):
        validate_transition(before, after, "qa", "T-002")


def test_validator_allows_qa_pending_pending():
    """QA may stay in pending while appending to its own attempt_log."""
    before = _tl(
        _dev("T-001", status="ready_for_qa"),
        _qa("T-002", source="follows T-001"),
    )
    after = _tl(
        _dev("T-001", status="ready_for_qa"),
        Item(id="T-002", type="qa", status="pending", title="qa T-002",
             source="follows T-001",
             attempt_log=[Attempt(3, "pending", "qa tool read failed — retry")]),
    )
    validate_transition(before, after, "qa", "T-002")  # should not raise


# ---------- PM decisions ------------------------------------------------


def test_pm_dispatches_dev_when_deps_done():
    tl = _tl(
        _dev("T-001", status="done"),
        _qa("T-002", source="follows T-001", status="done"),
        Item(id="T-003", type="dev", status="pending", title="T-003",
             dependencies=["T-001"]),
    )
    d = pm_agent.decide(tl)
    assert d.next == "dev"
    assert d.item_id == "T-003"


def test_pm_dispatches_qa_when_ready():
    tl = _tl(
        _dev("T-001", status="ready_for_qa"),
        _qa("T-002", source="follows T-001"),
    )
    d = pm_agent.decide(tl)
    assert d.next == "qa"
    assert d.item_id == "T-002"


def test_pm_done_when_all_done():
    tl = _tl(
        _dev("T-001", status="done"),
        _qa("T-002", source="follows T-001", status="done"),
    )
    d = pm_agent.decide(tl)
    assert d.next == "done"
    assert d.item_id is None


def test_pm_skips_dev_with_unmet_deps():
    tl = _tl(
        _dev("T-001", status="pending"),
        Item(id="T-002", type="dev", status="pending", title="t2",
             dependencies=["T-001"]),
    )
    d = pm_agent.decide(tl)
    # must pick T-001, not T-002
    assert d.next == "dev"
    assert d.item_id == "T-001"


# ---------- LoopState exhaustion ----------------------------------------


def test_loopstate_cycle_limit():
    s = LoopState(cycle=30)
    assert s.should_exit(Limits(max_cycles=30)) is not None


def test_loopstate_cost_limit():
    s = LoopState(total_cost_cny=1500)
    assert "cost" in (s.should_exit(Limits(max_cost_cny=1000)) or "")


def test_loopstate_stuck_pm():
    """v2: same_decision_count is tracked but no longer triggers early exit.
    Convergence now comes from fuse / reconcile / fingerprint_stuck."""
    s = LoopState()
    d = Decision(next="dev", item_id="T-001", reason="x")
    s.record_decision(d)
    s.record_decision(d)
    s.record_decision(d)
    assert s.same_decision_count == 3
    assert s.should_exit(Limits()) is None  # no longer exits on repeated decisions


def test_loopstate_persist_round_trip(tmp_path: Path):
    s = LoopState(cycle=5, total_cost_cny=42.5)
    s.record_decision(Decision(next="dev", item_id="T-003", reason="foo"))
    s.save(tmp_path)
    loaded = LoopState.load_or_init(tmp_path)
    assert loaded.cycle == 5
    assert loaded.total_cost_cny == 42.5
    assert loaded.last_decision is not None
    assert loaded.last_decision.item_id == "T-003"


def test_loopstate_resume_after_exhaust(tmp_path: Path):
    s = LoopState(cycle=3)
    s.mark_exhausted("max_cycles reached (3)")
    s.save(tmp_path)
    loaded = LoopState.load_or_init(tmp_path)
    assert loaded.exhausted_reason is not None
    # clear exhaustion for resume
    loaded.exhausted_reason = None
    assert loaded.should_exit(Limits(max_cycles=10)) is None


def test_state_load_legacy_json_no_new_fields(tmp_path: Path):
    """LoopState must tolerate state.json files written by v1 (no new fields)."""
    state_dir = tmp_path / ".agentloop"
    state_dir.mkdir()
    legacy = {
        "cycle": 7,
        "total_cost_cny": 3.5,
        "last_decision": {"next": "dev", "item_id": "T-003", "reason": "x"},
        "same_decision_count": 1,
        "started_at": "2026-04-01T00:00:00Z",
        "exhausted_reason": None,
        "rollbacks": [],
    }
    (state_dir / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
    loaded = LoopState.load_or_init(tmp_path)
    assert loaded.cycle == 7
    assert loaded.fingerprint_history == []
    assert loaded.abandoned_events == []
    assert loaded.scheduler_events == []
    assert loaded.planner_attempts == 0


def test_state_new_fields_persist_round_trip(tmp_path: Path):
    s = LoopState()
    s.fingerprint_history = ["abc", "def"]
    s.abandoned_events = [{"item_id": "T-001", "cycle": 3}]
    s.scheduler_events = [{"kind": "stale_doing_reconciled", "ids": ["T-007"]}]
    s.planner_attempts = 2
    s.save(tmp_path)
    loaded = LoopState.load_or_init(tmp_path)
    assert loaded.fingerprint_history == ["abc", "def"]
    assert loaded.abandoned_events == [{"item_id": "T-001", "cycle": 3}]
    assert loaded.scheduler_events == [{"kind": "stale_doing_reconciled", "ids": ["T-007"]}]
    assert loaded.planner_attempts == 2


def test_limits_new_defaults():
    lim = Limits()
    assert lim.max_planner_attempts == 3
    assert lim.max_fingerprint_stuck == 4


def test_exit_code_partial_success_exists():
    from agentloop.loop import ExitCode
    assert ExitCode.PARTIAL_SUCCESS.value == 3


def test_cli_tag_covers_partial_success(capsys):
    """cli._report_result must print PARTIAL_SUCCESS without KeyError."""
    from agentloop.cli import _report_result
    from agentloop.loop import ExitCode, LoopResult

    code = _report_result(LoopResult(ExitCode.PARTIAL_SUCCESS, "1 abandoned"))
    assert code == 3
    out = capsys.readouterr().out
    assert "PARTIAL_SUCCESS" in out
    assert "1 abandoned" in out


def test_server_derive_status_partial():
    """server/_derive_status_from_state should return 'partial' when PM said
    done and abandoned_events is non-empty."""
    from server.agentloop_manager import _derive_status_from_state

    partial_state = {
        "exhausted_reason": None,
        "last_decision": {"next": "done", "item_id": None, "reason": "1 item abandoned"},
        "abandoned_events": [{"item_id": "T-001", "cycle": 6}],
    }
    assert _derive_status_from_state(partial_state) == "partial"

    # all-done still maps to "done"
    done_state = {
        "exhausted_reason": None,
        "last_decision": {"next": "done", "item_id": None, "reason": "all done"},
        "abandoned_events": [],
    }
    assert _derive_status_from_state(done_state) == "done"


# ---------- rollback semantics (black-box: validator failure) -----------


def test_rollback_preserves_before_text(tmp_path: Path):
    """If validate fails, caller must restore todolist.md contents verbatim.

    We simulate the loop.run rollback branch: write `before`, invoke the
    agent (which we fake by writing an illegal state), then revert.
    """
    from agentloop.todolist import TODOLIST_FILE, parse, write as write_todolist

    before_tl = _tl(_dev("T-001"))
    write_todolist(tmp_path, before_tl)
    original_text = (tmp_path / TODOLIST_FILE).read_text(encoding="utf-8")

    # simulate an illegal dev write (pending → done direct)
    illegal = _tl(_dev("T-001", status="done"))
    write_todolist(tmp_path, illegal)

    after = parse(tmp_path)
    with pytest.raises(ValidationError):
        validate_transition(before_tl, after, "dev", "T-001")

    # caller restores
    (tmp_path / TODOLIST_FILE).write_text(original_text, encoding="utf-8")
    restored = parse(tmp_path)
    assert restored.by_id("T-001").status == "pending"


def test_fresh_wipes_state_but_keeps_config(tmp_path: Path):
    """--fresh must reset runs/state/todolist but preserve user config.toml."""
    from agentloop.loop import _wipe_agentloop_state
    from agentloop.todolist import TODOLIST_FILE

    state_dir = tmp_path / ".agentloop"
    state_dir.mkdir()
    (state_dir / "config.toml").write_text('[limits]\nmax_cycles = 99\n', encoding="utf-8")
    (state_dir / "state.json").write_text('{"cycle": 5}', encoding="utf-8")
    runs = state_dir / "runs"
    runs.mkdir()
    (runs / "001-planner.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / TODOLIST_FILE).write_text("# old\n", encoding="utf-8")

    _wipe_agentloop_state(tmp_path)

    assert (state_dir / "config.toml").exists()
    assert 'max_cycles = 99' in (state_dir / "config.toml").read_text(encoding="utf-8")
    assert not (state_dir / "state.json").exists()
    assert not runs.exists()
    assert not (tmp_path / TODOLIST_FILE).exists()
