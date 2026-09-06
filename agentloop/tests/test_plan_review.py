"""Tests for the human plan-review gate (plan_review + loop wiring).

Covers the four behaviors that make the gate trustworthy:
  * a fresh plan stops the loop at AWAITING_REVIEW (planner ran, phase 1 didn't)
  * approving lets phase 1 proceed without re-running the planner
  * editing the todolist and *then* approving executes the edited plan
  * editing after approval revokes it (drift detection)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import loop as scheduler
from agentloop import plan_review
from agentloop.config import AgentConfig, FeishuConfig, SummaryConfig
from agentloop.loop import ExitCode
from agentloop.workspace import WorkspacePaths


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

### T-002 · type:qa · status:pending
Verify T-001
- dependencies: [T-001]
- source: follows T-001
"""


def _ws(tmp_path: Path, slug: str = "gate-ws") -> WorkspacePaths:
    ws = WorkspacePaths.for_workspace(tmp_path, slug)
    ws.workspace_dir.mkdir(parents=True, exist_ok=True)
    return ws


def _quiet_config(policy: str = "always") -> AgentConfig:
    """Config with notifications off so tests never shell out to feishu-bot."""
    cfg = AgentConfig()
    cfg.summary_config = SummaryConfig(
        enabled=False, feishu_enabled=False, feishu=FeishuConfig()
    )
    cfg.review.plan = policy
    return cfg


@pytest.fixture
def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A workspace whose planner writes TODOLIST, with dispatch instrumented.

    Returns ``(ws, design, calls)`` where ``calls`` accumulates the roles the
    loop actually dispatched — the assertion surface for "planner ran once" and
    "phase 1 never started".
    """
    ws = _ws(tmp_path)
    design = tmp_path / "design.md"
    design.write_text("# Design\n", encoding="utf-8")
    ws.design.symlink_to(design)

    calls: list[str] = []

    def fake_planner_run(ws_, backend, design_path):
        calls.append("planner")
        ws_.todolist.write_text(TODOLIST, encoding="utf-8")
        from agentloop.agents.base import RunResult

        return RunResult(
            stream_json_path=Path("/dev/null"),
            duration_sec=0.0,
            cost_cny=0.0,
            success=True,
        )

    def fake_dispatch(decision, ws_, cycle, config):
        calls.append(f"dispatch:{decision.next}")
        raise AssertionError("phase 1 must not run while awaiting review")

    monkeypatch.setattr(scheduler.planner_agent, "run", fake_planner_run)
    monkeypatch.setattr(scheduler, "_dispatch", fake_dispatch)
    return ws, design, calls


# ---------- gate opens -----------------------------------------------------


def test_fresh_plan_stops_at_awaiting_review(gated, monkeypatch):
    ws, design, calls = gated
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, d: _quiet_config()))

    result = scheduler.run(design, ws=ws)

    assert result.code is ExitCode.AWAITING_REVIEW
    assert calls == ["planner"], "planner ran; phase 1 must not have dispatched"
    review = plan_review.PlanReview.load(ws)
    assert review is not None
    assert review.state == plan_review.AWAITING
    assert review.stats["items"] == 2
    assert review.stats["dev"] == 1
    assert review.stats["qa"] == 1
    # No checks field exists yet → every dev item counts as unverified.
    assert review.stats["unverified"] == 1
    assert review.stats["unverified_ids"] == ["T-001"]


def test_policy_never_skips_the_gate(gated, monkeypatch):
    ws, design, calls = gated
    monkeypatch.setattr(
        AgentConfig, "load", classmethod(lambda cls, d: _quiet_config("never"))
    )

    # _dispatch raises, proving we got into phase 1 rather than stopping.
    with pytest.raises(AssertionError, match="phase 1 must not run"):
        scheduler.run(design, ws=ws)

    assert plan_review.PlanReview.load(ws) is None, "no gate file when policy=never"


def test_relaunch_while_awaiting_does_not_rerun_planner(gated, monkeypatch):
    ws, design, calls = gated
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, d: _quiet_config()))

    first = scheduler.run(design, ws=ws)
    second = scheduler.run(design, ws=ws)

    assert first.code is second.code is ExitCode.AWAITING_REVIEW
    assert calls == ["planner"], "planner must run exactly once across relaunches"


# ---------- approval ------------------------------------------------------


def test_approval_lets_phase_one_start_without_replanning(gated, monkeypatch):
    ws, design, calls = gated
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, d: _quiet_config()))

    scheduler.run(design, ws=ws)
    plan_review.approve(ws)

    # _dispatch raising is the proof that phase 1 was entered.
    with pytest.raises(AssertionError, match="phase 1 must not run"):
        scheduler.run(design, ws=ws)

    assert calls.count("planner") == 1, "approval must not re-run the planner"
    assert "dispatch:dev" in calls
    assert plan_review.PlanReview.load(ws).state == plan_review.CONSUMED


def test_rejection_blocks_and_records_note(gated, monkeypatch):
    ws, design, calls = gated
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, d: _quiet_config()))

    scheduler.run(design, ws=ws)
    plan_review.reject(ws, note="T-001 太大，拆成三个")

    result = scheduler.run(design, ws=ws)

    assert result.code is ExitCode.AWAITING_REVIEW
    assert "rejected" in result.reason
    assert calls == ["planner"]
    note_file = plan_review.rejection_note_path(ws)
    assert note_file.exists()
    assert "拆成三个" in note_file.read_text(encoding="utf-8")


# ---------- digest binding ------------------------------------------------


def test_editing_then_approving_executes_the_edited_plan(gated, monkeypatch):
    """The primary 'reject' path: fix the plan in the UI, then approve."""
    ws, design, calls = gated
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, d: _quiet_config()))

    scheduler.run(design, ws=ws)

    edited = TODOLIST.replace("Implement the thing", "Implement the thing, scoped down")
    ws.todolist.write_text(edited, encoding="utf-8")
    plan_review.approve(ws)

    with pytest.raises(AssertionError, match="phase 1 must not run"):
        scheduler.run(design, ws=ws)

    assert calls.count("planner") == 1
    assert "scoped down" in ws.todolist.read_text(encoding="utf-8")
    review = plan_review.PlanReview.load(ws)
    assert review.state == plan_review.CONSUMED


def test_edit_after_approval_revokes_it(gated, monkeypatch):
    ws, design, calls = gated
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, d: _quiet_config()))

    scheduler.run(design, ws=ws)
    plan_review.approve(ws)

    # Someone edits the plan *after* signing off — the loop must not execute a
    # plan no human reviewed in this shape.
    ws.todolist.write_text(
        TODOLIST.replace("Implement the thing", "Something else entirely"),
        encoding="utf-8",
    )

    result = scheduler.run(design, ws=ws)

    assert result.code is ExitCode.AWAITING_REVIEW
    assert "changed after approval" in result.reason
    assert calls == ["planner"], "phase 1 must not run on a drifted plan"
    review = plan_review.PlanReview.load(ws)
    assert review.state == plan_review.AWAITING
    assert review.reviewed_at is None
    assert review.notified_at is None, "drift starts a new notify episode"

    # Re-approving the new content proceeds.
    plan_review.approve(ws)
    with pytest.raises(AssertionError, match="phase 1 must not run"):
        scheduler.run(design, ws=ws)


def test_consumed_gate_survives_loop_mutations(gated, monkeypatch):
    """A consumed gate must not re-trip when the loop itself edits the todolist.

    Statuses advance and attempt_logs grow during phase 1, so the digest will
    not match the approved bytes. Only ``consumed`` state prevents a spurious
    re-approval demand on resume.
    """
    ws, design, calls = gated
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, d: _quiet_config()))

    scheduler.run(design, ws=ws)
    plan_review.approve(ws)
    with pytest.raises(AssertionError):
        scheduler.run(design, ws=ws)

    # Simulate the loop having advanced an item.
    ws.todolist.write_text(
        TODOLIST.replace("status:pending", "status:ready_for_qa", 1), encoding="utf-8"
    )

    gate = plan_review.check_gate(ws, enabled=True)
    assert gate.proceed is True
    assert gate.reverted is False


# ---------- policy: when_unverified ---------------------------------------


def test_when_unverified_gates_because_no_checks_exist_yet(gated, monkeypatch):
    ws, design, calls = gated
    monkeypatch.setattr(
        AgentConfig,
        "load",
        classmethod(lambda cls, d: _quiet_config("when_unverified")),
    )

    result = scheduler.run(design, ws=ws)

    # Until the evidence gate lands, every dev item is unverified → gate on.
    assert result.code is ExitCode.AWAITING_REVIEW
    assert calls == ["planner"]


def test_open_gate_is_consulted_even_when_policy_would_not_reopen_it(
    gated, monkeypatch
):
    """The policy decides whether to *open* a gate, never whether to honor one.

    Under ``when_unverified``, an edit that leaves no unverified dev items would
    otherwise flip the policy off and skip the awaiting check entirely — running
    the unapproved edit.
    """
    ws, design, calls = gated
    monkeypatch.setattr(
        AgentConfig,
        "load",
        classmethod(lambda cls, d: _quiet_config("when_unverified")),
    )

    assert scheduler.run(design, ws=ws).code is ExitCode.AWAITING_REVIEW

    # Strip the dev item so summarize() reports 0 unverified → policy says
    # "don't gate". The awaiting gate on disk must still stop us.
    ws.todolist.write_text(
        """---
project: demo
design_doc: design.md
created_at: 2026-09-05T00:00:00Z
cycle: 0
---

# Todolist

## Items

### T-002 · type:qa · status:pending
Verify something
- dependencies: []
""",
        encoding="utf-8",
    )

    result = scheduler.run(design, ws=ws)

    assert result.code is ExitCode.AWAITING_REVIEW
    assert calls == ["planner"], "phase 1 must not run on an unapproved edit"


# ---------- config parsing ------------------------------------------------


def test_config_parses_review_policy(tmp_path: Path):
    (tmp_path / "config.toml").write_text(
        '[review]\nplan = "never"\n', encoding="utf-8"
    )
    assert AgentConfig.load(tmp_path).review.plan == "never"


def test_config_rejects_unknown_policy_and_keeps_default(tmp_path: Path):
    """A typo must not silently disable the gate."""
    (tmp_path / "config.toml").write_text(
        '[review]\nplan = "nevr"\n', encoding="utf-8"
    )
    assert AgentConfig.load(tmp_path).review.plan == "always"


def test_review_plan_flag_forces_gate_on(tmp_path: Path):
    cfg = _quiet_config("never")
    cfg.review_plan = True
    from agentloop.todolist import Todolist

    assert scheduler._gate_enabled(cfg, Todolist()) is True


# ---------- digest helper -------------------------------------------------


def test_digest_is_empty_without_todolist(tmp_path: Path):
    assert plan_review.todolist_digest(_ws(tmp_path)) == ""


def test_digest_tracks_raw_bytes(tmp_path: Path):
    ws = _ws(tmp_path)
    ws.todolist.write_text(TODOLIST, encoding="utf-8")
    first = plan_review.todolist_digest(ws)
    # Whitespace-only change must still invalidate: the reviewer approved bytes.
    ws.todolist.write_text(TODOLIST + "\n", encoding="utf-8")
    assert plan_review.todolist_digest(ws) != first


def test_corrupt_gate_file_reads_as_absent(tmp_path: Path):
    ws = _ws(tmp_path)
    plan_review.review_path(ws).write_text("{not json", encoding="utf-8")
    assert plan_review.PlanReview.load(ws) is None
    # ...but the file's *presence* is still observable, which is what lets
    # check_gate fail closed instead of reading it as "no gate".
    assert plan_review.gate_file_present(ws) is True


def test_corrupt_gate_file_fails_closed(tmp_path: Path):
    """A truncated gate must never let an unapproved plan execute."""
    ws = _ws(tmp_path)
    ws.todolist.write_text(TODOLIST, encoding="utf-8")
    plan_review.review_path(ws).write_text('{"state": "appro', encoding="utf-8")

    gate = plan_review.check_gate(ws, enabled=True)

    assert gate.proceed is False
    assert "unreadable" in gate.reason


def test_missing_gate_file_still_proceeds(tmp_path: Path):
    """Legacy workspaces (planner ran before the gate existed) are unaffected."""
    ws = _ws(tmp_path)
    ws.todolist.write_text(TODOLIST, encoding="utf-8")
    assert plan_review.check_gate(ws, enabled=True).proceed is True


def test_save_leaves_no_partial_file_behind(tmp_path: Path):
    """save() is atomic: the temp file is renamed, not left as a sibling."""
    ws = _ws(tmp_path)
    plan_review.PlanReview(state=plan_review.AWAITING).save(ws)
    leftovers = [p.name for p in ws.workspace_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    assert plan_review.PlanReview.load(ws).state == plan_review.AWAITING


def test_approve_refuses_an_all_done_plan(tmp_path: Path):
    """An edited all-done plan would consume the gate and report SUCCESS
    without executing anything — the planner invariant already forbids it."""
    ws = _ws(tmp_path)
    ws.todolist.write_text(
        TODOLIST.replace("status:pending", "status:done"), encoding="utf-8"
    )
    plan_review.PlanReview(state=plan_review.AWAITING).save(ws)

    with pytest.raises(plan_review.PlanReviewError, match="not a valid initial plan"):
        plan_review.approve(ws)

    assert plan_review.PlanReview.load(ws).state == plan_review.AWAITING


def test_approve_refuses_duplicate_item_ids(tmp_path: Path):
    ws = _ws(tmp_path)
    ws.todolist.write_text(TODOLIST.replace("T-002", "T-001"), encoding="utf-8")
    plan_review.PlanReview(state=plan_review.AWAITING).save(ws)

    with pytest.raises(plan_review.PlanReviewError, match="duplicate"):
        plan_review.approve(ws)


def test_approve_refuses_unknown_item_type(tmp_path: Path):
    ws = _ws(tmp_path)
    ws.todolist.write_text(TODOLIST.replace("type:qa", "type:wat"), encoding="utf-8")
    plan_review.PlanReview(state=plan_review.AWAITING).save(ws)

    with pytest.raises(plan_review.PlanReviewError, match="not a valid initial plan"):
        plan_review.approve(ws)


def test_gate_with_non_mapping_stats_reads_as_unreadable(tmp_path: Path):
    """A hand-edited gate must fail closed, not raise TypeError out of load()."""
    ws = _ws(tmp_path)
    ws.todolist.write_text(TODOLIST, encoding="utf-8")
    plan_review.review_path(ws).write_text(
        '{"state": "approved", "stats": 1}', encoding="utf-8"
    )

    assert plan_review.PlanReview.load(ws) is None
    gate = plan_review.check_gate(ws, enabled=True)
    assert gate.proceed is False
    assert "unreadable" in gate.reason


def test_approve_without_gate_raises(tmp_path: Path):
    with pytest.raises(plan_review.PlanReviewError):
        plan_review.approve(_ws(tmp_path))


def test_approve_refuses_an_empty_plan(tmp_path: Path):
    """Approving an empty todolist would report SUCCESS having done nothing:
    the file exists so the planner is skipped, PM immediately says done."""
    ws = _ws(tmp_path)
    ws.todolist.write_text(TODOLIST, encoding="utf-8")
    plan_review.PlanReview(state=plan_review.AWAITING).save(ws)
    ws.todolist.write_text("", encoding="utf-8")

    with pytest.raises(plan_review.PlanReviewError, match="no items"):
        plan_review.approve(ws)

    assert plan_review.PlanReview.load(ws).state == plan_review.AWAITING


def test_approve_refuses_a_missing_plan(tmp_path: Path):
    ws = _ws(tmp_path)
    plan_review.PlanReview(state=plan_review.AWAITING).save(ws)

    with pytest.raises(plan_review.PlanReviewError, match="no todolist"):
        plan_review.approve(ws)


def test_approve_refreshes_stats_from_the_approved_plan(tmp_path: Path):
    ws = _ws(tmp_path)
    ws.todolist.write_text(TODOLIST, encoding="utf-8")
    plan_review.PlanReview(state=plan_review.AWAITING, stats={"items": 99}).save(ws)

    review = plan_review.approve(ws)

    assert review.stats["items"] == 2


def test_reject_on_a_consumed_gate_raises(tmp_path: Path):
    """A stale reject must not flip a finished loop's gate back to rejected —
    status derivation prioritizes the gate over the terminal state."""
    ws = _ws(tmp_path)
    ws.todolist.write_text(TODOLIST, encoding="utf-8")
    plan_review.PlanReview(state=plan_review.CONSUMED).save(ws)

    with pytest.raises(plan_review.PlanReviewError, match="consumed"):
        plan_review.reject(ws, note="too late")

    assert plan_review.PlanReview.load(ws).state == plan_review.CONSUMED
    assert not plan_review.rejection_note_path(ws).exists()


# ---------- notification retry --------------------------------------------


def test_failed_card_send_does_not_suppress_the_retry(gated, monkeypatch):
    ws, design, calls = gated
    cfg = _quiet_config()
    cfg.summary_config = SummaryConfig(
        enabled=True, feishu_enabled=True, feishu=FeishuConfig()
    )
    cfg.review.plan = "always"
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, d: cfg))

    sends: list[int] = []
    monkeypatch.setattr(
        scheduler.notify, "send_feishu_card", lambda c, m: sends.append(1) or False
    )

    scheduler.run(design, ws=ws)
    assert plan_review.PlanReview.load(ws).notified_at is None

    scheduler.run(design, ws=ws)
    assert len(sends) == 2, "a failed send must be retried on relaunch"


def test_successful_card_send_suppresses_the_retry(gated, monkeypatch):
    ws, design, calls = gated
    cfg = _quiet_config()
    cfg.summary_config = SummaryConfig(
        enabled=True, feishu_enabled=True, feishu=FeishuConfig()
    )
    cfg.review.plan = "always"
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, d: cfg))

    sends: list[int] = []
    monkeypatch.setattr(
        scheduler.notify, "send_feishu_card", lambda c, m: sends.append(1) or True
    )

    scheduler.run(design, ws=ws)
    assert plan_review.PlanReview.load(ws).notified_at

    scheduler.run(design, ws=ws)
    assert len(sends) == 1, "a delivered card must not be re-sent"


# ---------- rejection feedback survives --fresh ---------------------------


def test_fresh_preserves_rejection_note(gated, monkeypatch):
    """--fresh must not delete the reviewer's reason for the last rejection."""
    ws, design, calls = gated
    monkeypatch.setattr(AgentConfig, "load", classmethod(lambda cls, d: _quiet_config()))

    scheduler.run(design, ws=ws)
    plan_review.reject(ws, note="T-001 太大，拆成三个")

    scheduler.run(design, fresh=True, ws=ws)

    # The planner re-ran (todolist was wiped), and got the note in its prompt.
    assert calls == ["planner", "planner"]
    # Consumed after the successful re-plan so it isn't re-injected forever.
    assert not plan_review.rejection_note_path(ws).exists()


def test_planner_prompt_carries_the_rejection_note(tmp_path: Path, monkeypatch):
    ws = _ws(tmp_path)
    design = tmp_path / "design.md"
    design.write_text("# Design\n", encoding="utf-8")
    plan_review.rejection_note_path(ws).write_text(
        "# Plan rejected\n\nT-001 太大，拆成三个\n", encoding="utf-8"
    )

    seen: dict[str, str] = {}

    def fake_run_agent(role, ws_, item, backend, prompt):
        seen["prompt"] = prompt
        from agentloop.agents.base import RunResult

        return RunResult(
            stream_json_path=Path("/dev/null"),
            duration_sec=0.0,
            cost_cny=0.0,
            success=True,
        )

    from agentloop.agents import planner as planner_agent

    monkeypatch.setattr(planner_agent, "run_agent", fake_run_agent)
    planner_agent.run(ws, _quiet_config().planner, design)

    assert "拆成三个" in seen["prompt"]
    assert "驳回" in seen["prompt"]


def test_planner_prompt_omits_section_without_a_note(tmp_path: Path, monkeypatch):
    ws = _ws(tmp_path)
    design = tmp_path / "design.md"
    design.write_text("# Design\n", encoding="utf-8")

    seen: dict[str, str] = {}

    def fake_run_agent(role, ws_, item, backend, prompt):
        seen["prompt"] = prompt
        from agentloop.agents.base import RunResult

        return RunResult(
            stream_json_path=Path("/dev/null"),
            duration_sec=0.0,
            cost_cny=0.0,
            success=True,
        )

    from agentloop.agents import planner as planner_agent

    monkeypatch.setattr(planner_agent, "run_agent", fake_run_agent)
    planner_agent.run(ws, _quiet_config().planner, design)

    assert "驳回" not in seen["prompt"]
