"""Scheduler loop — the heart of agentloop.

Implements the pseudo-code from DESIGN §6:

    1. Phase 0: if no todolist exists, run planner.
    2. Phase 1: while not done:
         - PM decides next actor
         - run agent
         - validate transition (rollback on failure)
         - update state, persist
    3. Exit when PM says "done" or LoopState.should_exit() fires.
"""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .agents import dev as dev_agent
from .agents import planner as planner_agent
from .agents import pm as pm_agent
from .agents import qa as qa_agent
from .agents.base import RunResult
from .config import AgentConfig
from .state import Decision, LoopState
from .todolist import Todolist, TODOLIST_FILE, parse as parse_todolist, write as write_todolist
from .validator import ValidationError, validate_transition

logger = logging.getLogger(__name__)


class ExitCode(Enum):
    SUCCESS = 0
    EXHAUSTED = 1
    ERROR = 2


@dataclass
class LoopResult:
    code: ExitCode
    reason: str = ""


def run(
    design_path: Path,
    *,
    fresh: bool = False,
    review_plan: bool | None = None,
    max_cycles: int | None = None,
    max_cost_cny: float | None = None,
) -> LoopResult:
    cwd = design_path.parent.resolve()
    if not design_path.exists():
        return LoopResult(ExitCode.ERROR, f"design not found: {design_path}")

    if fresh:
        _wipe_agentloop_state(cwd)

    state = LoopState.load_or_init(cwd)
    config = AgentConfig.load(cwd)
    if review_plan is not None:
        config.review_plan = review_plan
    if max_cycles is not None:
        config.limits.max_cycles = max_cycles
    if max_cost_cny is not None:
        config.limits.max_cost_cny = max_cost_cny

    if state.exhausted_reason:
        # Resume needs explicit budget bump; by default we stay exhausted.
        return LoopResult(ExitCode.EXHAUSTED, state.exhausted_reason)

    # --- phase 0: planner ------------------------------------------------
    todolist_path = cwd / TODOLIST_FILE
    if not todolist_path.exists():
        logger.info("no todolist — running planner")
        before = Todolist()  # empty
        result = planner_agent.run(cwd, config.planner)
        state.record_cost(result.cost_cny)
        after = parse_todolist(cwd)
        try:
            validate_transition(before, after, "planner", None)
        except ValidationError as e:
            # Remove the malformed todolist so a later `resume` re-enters
            # phase 0 with a clean slate instead of skipping planner and
            # continuing from invalid state.
            try:
                todolist_path.unlink()
            except FileNotFoundError:
                pass
            state.mark_exhausted(f"planner validation failed: {e}")
            state.save(cwd)
            return LoopResult(ExitCode.ERROR, str(e))
        if not result.success:
            state.mark_exhausted("planner failed (non-zero exit or stream error)")
            state.save(cwd)
            return LoopResult(ExitCode.ERROR, "planner failed — see .agentloop/runs/")
        state.save(cwd)

        if config.review_plan:
            try:
                input("[agentloop] planner finished — press Enter to start the loop... ")
            except EOFError:
                pass

    # --- phase 1: scheduling loop ---------------------------------------
    while True:
        todolist = parse_todolist(cwd)

        if reason := state.should_exit(config.limits):
            state.mark_exhausted(reason)
            state.save(cwd)
            return LoopResult(ExitCode.EXHAUSTED, reason)

        # exhaust on per-item attempt ceiling
        stuck_id = _item_over_attempt_limit(todolist, config.limits.max_item_attempts)
        if stuck_id:
            reason = f"item {stuck_id} exceeded max_item_attempts"
            state.mark_exhausted(reason)
            state.save(cwd)
            return LoopResult(ExitCode.EXHAUSTED, reason)

        decision = pm_agent.decide(todolist)
        state.record_decision(decision)

        if decision.next == "done":
            if todolist.items and all(it.status == "done" for it in todolist.items):
                state.save(cwd)
                return LoopResult(ExitCode.SUCCESS, decision.reason or "all done")
            # PM said done but items remain non-done → we're exhausting with
            # nothing actionable left. Persist that so `resume` knows not to
            # silently re-enter the loop.
            reason = decision.reason or "no actionable items"
            state.mark_exhausted(reason)
            state.save(cwd)
            return LoopResult(ExitCode.EXHAUSTED, reason)

        if state.same_decision_count >= 3:
            reason = (
                f"PM stuck on ({decision.next}, {decision.item_id}) — "
                "3 consecutive identical decisions"
            )
            state.mark_exhausted(reason)
            state.save(cwd)
            return LoopResult(ExitCode.EXHAUSTED, reason)

        logger.info(
            "cycle %d: %s → %s (%s)",
            state.cycle + 1, decision.next, decision.item_id, decision.reason,
        )

        before_text = todolist_path.read_text(encoding="utf-8") if todolist_path.exists() else ""
        result = _dispatch(decision, cwd, state.cycle + 1, config)
        state.record_cost(result.cost_cny)

        after = parse_todolist(cwd)
        try:
            validate_transition(todolist, after, decision.next, decision.item_id)
        except ValidationError as e:
            logger.warning("validation failed: %s — rolling back todolist", e)
            todolist_path.write_text(before_text, encoding="utf-8")
            state.record_rollback(decision.next, decision.item_id, str(e))

        state.cycle += 1
        state.save(cwd)


# ----- helpers ------------------------------------------------------------


def _dispatch(
    decision: Decision,
    cwd: Path,
    cycle: int,
    config: AgentConfig,
) -> RunResult:
    if decision.next == "dev":
        if decision.item_id is None:
            return _null_result("dev decision without item_id")
        return dev_agent.run(cwd, decision.item_id, cycle, config.dev)
    if decision.next == "qa":
        if decision.item_id is None:
            return _null_result("qa decision without item_id")
        return qa_agent.run(cwd, decision.item_id, cycle, config.qa)
    return _null_result(f"unknown decision.next={decision.next!r}")


def _null_result(msg: str) -> RunResult:
    return RunResult(
        stream_json_path=Path("/dev/null"),
        duration_sec=0.0,
        cost_cny=0.0,
        success=False,
        errors=[msg],
    )


def _item_over_attempt_limit(todolist: Todolist, limit: int) -> str | None:
    for it in todolist.items:
        if it.status == "done":
            continue
        # failed attempts = attempt_log entries whose result == "pending"
        failures = sum(1 for a in it.attempt_log if a.result == "pending")
        if failures >= limit:
            return it.id
    return None


def _wipe_agentloop_state(cwd: Path) -> None:
    """Wipe run state but preserve user config.toml.

    `--fresh` should reset the loop's work (todolist, state.json, runs logs)
    without erasing the user's backend/limits configuration.
    """
    state_dir = cwd / ".agentloop"
    if state_dir.exists():
        for child in state_dir.iterdir():
            if child.name == "config.toml":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    todolist = cwd / TODOLIST_FILE
    if todolist.exists():
        todolist.unlink()
