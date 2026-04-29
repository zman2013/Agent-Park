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
from . import scheduler_writes as sw
from .state import Decision, LoopState
from .todolist import Item, Todolist, TODOLIST_FILE, parse as parse_todolist, write as write_todolist
from .validator import ValidationError, _reviewed_dev_id, validate_transition

logger = logging.getLogger(__name__)


class ExitCode(Enum):
    SUCCESS = 0
    EXHAUSTED = 1
    ERROR = 2
    PARTIAL_SUCCESS = 3


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
    _startup_health(cwd, state)

    while True:
        todolist = parse_todolist(cwd)

        if reason := state.should_exit(config.limits):
            state.mark_exhausted(reason)
            state.save(cwd)
            return LoopResult(ExitCode.EXHAUSTED, reason)

        # v2: per-item fuse + cascade run before PM sees the todolist so that
        # PM and the attempt-limit check never operate on abandoned-but-live
        # items. `_fuse` turns over-limit items abandoned; `_cascade` closes
        # the downstream DAG and downgrades stranded qa/dev pairs.
        fused = _fuse(cwd, todolist, config.limits.max_item_attempts, state.cycle)
        cascaded = _cascade(cwd, todolist, state.cycle)
        if fused or cascaded:
            for iid, reason_txt in fused + cascaded:
                state.abandoned_events.append(
                    {
                        "cycle": state.cycle,
                        "item_id": iid,
                        "reason": reason_txt,
                        "at": _utcnow_iso(),
                    }
                )
            sw.scheduler_write(cwd, todolist)
            # re-parse so later checks see canonical on-disk state
            todolist = parse_todolist(cwd)

        decision = pm_agent.decide(todolist)
        state.record_decision(decision)

        if decision.next == "done":
            exit_code, reason = _classify_terminal(todolist, decision)
            if exit_code == ExitCode.SUCCESS:
                state.save(cwd)
                return LoopResult(exit_code, reason)
            if exit_code == ExitCode.PARTIAL_SUCCESS:
                state.save(cwd)
                return LoopResult(exit_code, reason)
            # EXHAUSTED — nothing actionable left but some items still open
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


def _startup_health(cwd: Path, state: LoopState) -> None:
    """Run once before entering the main loop.

    * Dep cycles / dangling deps → mark exhausted immediately (these are
      structural planner bugs the loop can't recover from without human edit).
    * Stale ``doing`` items left over from a prior crashed run → reset to
      ``pending`` via a scheduler-write. We do this before any PM decision
      so PM never sees ``doing`` (Inv-D).
    """
    tl = parse_todolist(cwd)
    if not tl.items:
        return
    cycles = sw.find_dep_cycles(tl)
    if cycles:
        state.mark_exhausted(
            "dependency cycle: " + " | ".join("→".join(c) for c in cycles)
        )
        state.save(cwd)
        return
    dangling = sw.find_dangling_deps(tl)
    if dangling:
        pairs = ", ".join(f"{a} → {b}" for a, b in dangling)
        state.mark_exhausted(f"dangling dependencies: {pairs}")
        state.save(cwd)
        return
    reset = sw.stale_doing_reconcile(tl, boot_cycle=state.cycle)
    if reset:
        sw.scheduler_write(cwd, tl)
        state.scheduler_events.append(
            {
                "cycle": state.cycle,
                "kind": "stale_doing_reconciled",
                "ids": reset,
                "at": _utcnow_iso(),
            }
        )
        state.save(cwd)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _item_failures(item: Item) -> int:
    """Number of failed (pending-result) attempts in an item's attempt_log."""
    return sum(1 for a in item.attempt_log if a.result == "pending")


def _fuse(cwd: Path, tl: Todolist, max_attempts: int, cycle: int) -> list[tuple[str, str]]:
    """Mark items whose attempt_log has ≥ max_attempts failures as abandoned.

    Returns list of (item_id, reason) for each newly abandoned item. The
    todolist is mutated in place; caller is responsible for scheduler_write.
    Skips already-terminal items so repeated passes are idempotent.
    """
    newly: list[tuple[str, str]] = []
    for it in tl.items:
        if it.status in {"done", "abandoned"}:
            continue
        failures = _item_failures(it)
        if failures < max_attempts:
            continue
        reason = f"exceeded max_item_attempts ({failures}/{max_attempts})"
        if sw.mark_abandoned(tl, it.id, reason, cycle):
            newly.append((it.id, reason))
    return newly


def _cascade(cwd: Path, tl: Todolist, cycle: int) -> list[tuple[str, str]]:
    """Cascade ``abandoned`` through downstream deps; downgrade stranded qa-dev.

    Fixed-point iteration: anything whose dep is abandoned becomes abandoned
    itself. Independent DAG branches survive. After propagation, any qa item
    already in ``abandoned`` causes its paired ``ready_for_qa`` dev to roll
    back to ``pending`` (give retry-and-fuse another shot before final fuse).
    Returns list of (item_id, reason) for newly abandoned items.
    """
    newly: list[tuple[str, str]] = []
    abandoned = {it.id for it in tl.items if it.status == "abandoned"}

    changed = True
    while changed:
        changed = False
        for it in tl.items:
            if it.status in {"done", "abandoned"}:
                continue
            bad = [d for d in it.dependencies if d in abandoned]
            if not bad:
                continue
            reason = f"cascade: dep(s) abandoned: {', '.join(bad)}"
            if sw.mark_abandoned(tl, it.id, reason, cycle):
                abandoned.add(it.id)
                newly.append((it.id, reason))
                changed = True

    # qa abandoned → downgrade reviewed dev back to pending
    for it in tl.items:
        if it.type != "qa" or it.status != "abandoned":
            continue
        dev_id = _reviewed_dev_id(it, tl)
        if not dev_id:
            continue
        dev = tl.by_id(dev_id)
        if dev is None or dev.status != "ready_for_qa":
            continue
        sw.downgrade_reviewed_dev(
            tl, dev_id, cycle, f"qa {it.id} abandoned"
        )
    return newly


def _classify_terminal(
    tl: Todolist, decision: Decision
) -> tuple["ExitCode", str]:
    """Map PM's ``done`` decision to a concrete ExitCode + reason.

    * all items done → SUCCESS
    * all items {done, abandoned} with ≥1 abandoned → PARTIAL_SUCCESS
    * anything else → EXHAUSTED (PM said done but items remain live)
    """
    if not tl.items:
        return ExitCode.SUCCESS, decision.reason or "empty todolist"
    statuses = {it.status for it in tl.items}
    if statuses == {"done"}:
        return ExitCode.SUCCESS, decision.reason or "all items done"
    if statuses <= {"done", "abandoned"}:
        n = sum(1 for it in tl.items if it.status == "abandoned")
        return ExitCode.PARTIAL_SUCCESS, f"{n} item(s) abandoned, rest done"
    return ExitCode.EXHAUSTED, decision.reason or "no actionable items"


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
