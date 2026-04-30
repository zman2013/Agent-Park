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
from .validator import ValidationError, _reviewed_dev_ids, validate_transition
from .workspace import WorkspacePaths

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
    ws: WorkspacePaths,
) -> LoopResult:
    """Run or resume the scheduler.

    ``ws`` — workspace resolved by the CLI or agentloop manager. state/todolist
    /runs live under ``ws.workspace_dir``, which is also the subprocess cwd for
    all agent invocations.
    """
    if not design_path.exists():
        return LoopResult(ExitCode.ERROR, f"design not found: {design_path}")

    if fresh:
        _wipe_agentloop_state(ws)

    state = LoopState.load_or_init(ws)
    config = AgentConfig.load(ws.workspace_dir)
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
    todolist_path = ws.todolist
    if not todolist_path.exists():
        logger.info("no todolist — running planner")
        planner_result = _run_planner_with_retry(
            ws, config, state, todolist_path, design_path
        )
        if planner_result is not None:
            return planner_result

        if config.review_plan:
            try:
                input("[agentloop] planner finished — press Enter to start the loop... ")
            except EOFError:
                pass

    # --- phase 1: scheduling loop ---------------------------------------
    _startup_health(ws, state)

    while True:
        todolist = parse_todolist(ws)

        if reason := state.should_exit(config.limits):
            state.mark_exhausted(reason)
            state.save(ws)
            return LoopResult(ExitCode.EXHAUSTED, reason)

        # v2: per-item fuse + cascade run before PM sees the todolist so that
        # PM and the attempt-limit check never operate on abandoned-but-live
        # items. `_fuse` turns over-limit items abandoned; `_cascade` closes
        # the downstream DAG and downgrades stranded qa/dev pairs.
        fused = _fuse(ws, todolist, config.limits.max_item_attempts, state.cycle)
        cascaded, downgraded = _cascade(ws, todolist, state.cycle)
        if fused or cascaded or downgraded:
            for iid, reason_txt in fused + cascaded:
                state.abandoned_events.append(
                    {
                        "cycle": state.cycle,
                        "item_id": iid,
                        "reason": reason_txt,
                        "at": _utcnow_iso(),
                    }
                )
            for iid, reason_txt in downgraded:
                state.scheduler_events.append(
                    {
                        "cycle": state.cycle,
                        "kind": "qa_abandoned_downgrade",
                        "dev_id": iid,
                        "reason": reason_txt,
                        "at": _utcnow_iso(),
                    }
                )
            sw.scheduler_write(ws, todolist)
            # re-parse so later checks see canonical on-disk state
            todolist = parse_todolist(ws)

        decision = pm_agent.decide(todolist)

        # v2: dynamic qa creation when PM points at a stranded ready_for_qa dev
        if decision.next == "qa" and decision.item_id is None:
            dev_id = _extract_dev_id_from_reason(decision.reason)
            if dev_id and todolist.by_id(dev_id):
                new_qa = sw.create_dynamic_qa(todolist, dev_id, state.cycle)
                sw.scheduler_write(ws, todolist)
                state.scheduler_events.append(
                    {
                        "cycle": state.cycle,
                        "kind": "dynamic_qa_created",
                        "qa_id": new_qa.id,
                        "for_dev": dev_id,
                        "at": _utcnow_iso(),
                    }
                )
                todolist = parse_todolist(ws)
                decision = pm_agent.decide(todolist)

        state.record_decision(decision)

        if decision.next == "done":
            exit_code, reason = _classify_terminal(todolist, decision)
            if exit_code == ExitCode.SUCCESS:
                state.save(ws)
                return LoopResult(exit_code, reason)
            if exit_code == ExitCode.PARTIAL_SUCCESS:
                state.save(ws)
                return LoopResult(exit_code, reason)
            # EXHAUSTED — nothing actionable left but some items still open
            state.mark_exhausted(reason)
            state.save(ws)
            return LoopResult(ExitCode.EXHAUSTED, reason)

        # v2: removed the "3 consecutive identical PM decisions" early exit.
        # PM is deterministic and will keep re-dispatching the head-of-queue
        # item across retries; the convergence guarantees now come from
        # `_fuse` (attempt_log ceiling), `_reconcile` (silent failure → log
        # entry), and `_fingerprint_stuck` (structural no-progress over N
        # cycles). Keeping `same_decision_count` on LoopState for debugging.

        logger.info(
            "cycle %d: %s → %s (%s)",
            state.cycle + 1, decision.next, decision.item_id, decision.reason,
        )

        before_text = todolist_path.read_text(encoding="utf-8") if todolist_path.exists() else ""
        before_tl = todolist
        result = _dispatch(decision, ws, state.cycle + 1, config)
        state.record_cost(result.cost_cny)

        after = parse_todolist(ws)
        try:
            validate_transition(before_tl, after, decision.next, decision.item_id)
        except ValidationError as e:
            logger.warning("validation failed: %s — rolling back todolist", e)
            todolist_path.write_text(before_text, encoding="utf-8")
            state.record_rollback(decision.next, decision.item_id, str(e))
            after = parse_todolist(ws)

        # v2: reconcile — if the dispatched agent failed to advance the item
        # (stalled, crashed, or silently exited without a state change),
        # scheduler writes a pending attempt so the fuse counter moves.
        if not _decision_advanced(before_tl, after, decision):
            _reconcile(ws, after, decision, result, state.cycle + 1)

        # v2: fingerprint stuck check — drives real structural progress even
        # when individual cycles look like they make headway via attempt_log
        # growth but nothing terminal happens.
        fp = _fingerprint(after)
        state.fingerprint_history.append(fp)
        if _fingerprint_stuck(state.fingerprint_history, config.limits.max_fingerprint_stuck):
            reason = (
                f"structural fingerprint unchanged for "
                f"{config.limits.max_fingerprint_stuck} cycles"
            )
            state.mark_exhausted(reason)
            state.cycle += 1
            state.save(ws)
            return LoopResult(ExitCode.EXHAUSTED, reason)

        state.cycle += 1
        state.save(ws)


# ----- helpers ------------------------------------------------------------


def _startup_health(ws: WorkspacePaths, state: LoopState) -> None:
    """Run once before entering the main loop.

    * Dep cycles / dangling deps → mark exhausted immediately (these are
      structural planner bugs the loop can't recover from without human edit).
    * Stale ``doing`` items left over from a prior crashed run → reset to
      ``pending`` via a scheduler-write. We do this before any PM decision
      so PM never sees ``doing`` (Inv-D).
    """
    tl = parse_todolist(ws)
    if not tl.items:
        return
    cycles = sw.find_dep_cycles(tl)
    if cycles:
        state.mark_exhausted(
            "dependency cycle: " + " | ".join("→".join(c) for c in cycles)
        )
        state.save(ws)
        return
    dangling = sw.find_dangling_deps(tl)
    if dangling:
        pairs = ", ".join(f"{a} → {b}" for a, b in dangling)
        state.mark_exhausted(f"dangling dependencies: {pairs}")
        state.save(ws)
        return
    reset = sw.stale_doing_reconcile(tl, boot_cycle=state.cycle)
    if reset:
        sw.scheduler_write(ws, tl)
        state.scheduler_events.append(
            {
                "cycle": state.cycle,
                "kind": "stale_doing_reconciled",
                "ids": reset,
                "at": _utcnow_iso(),
            }
        )
        state.save(ws)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dispatch(
    decision: Decision,
    ws: WorkspacePaths,
    cycle: int,
    config: AgentConfig,
) -> RunResult:
    if decision.next == "dev":
        if decision.item_id is None:
            return _null_result("dev decision without item_id")
        return dev_agent.run(ws, decision.item_id, cycle, config.dev)
    if decision.next == "qa":
        if decision.item_id is None:
            return _null_result("qa decision without item_id")
        return qa_agent.run(ws, decision.item_id, cycle, config.qa)
    return _null_result(f"unknown decision.next={decision.next!r}")


def _null_result(msg: str) -> RunResult:
    return RunResult(
        stream_json_path=Path("/dev/null"),
        duration_sec=0.0,
        cost_cny=0.0,
        success=False,
        errors=[msg],
    )


def _run_planner_with_retry(
    ws: WorkspacePaths,
    config: AgentConfig,
    state: LoopState,
    todolist_path: Path,
    design_path: Path,
) -> LoopResult | None:
    """Run planner up to limits.max_planner_attempts; return LoopResult on fatal, else None.

    A successful attempt returns None so the caller continues into phase 1.
    On each failure we unlink any partial todolist so the next attempt starts
    clean. Final failure marks state exhausted with ExitCode.ERROR.
    """
    before = Todolist()  # phase 0 only runs when todolist is empty
    last_error = ""
    for attempt in range(config.limits.max_planner_attempts):
        state.planner_attempts = attempt + 1
        result = planner_agent.run(ws, config.planner, design_path)
        state.record_cost(result.cost_cny)
        after = parse_todolist(ws)
        try:
            validate_transition(before, after, "planner", None)
            if result.success:
                state.save(ws)
                return None
            last_error = "planner failed (non-zero exit or stream error)"
        except ValidationError as e:
            last_error = f"planner validation failed: {e}"
        logger.warning(
            "planner attempt %d/%d failed: %s",
            attempt + 1, config.limits.max_planner_attempts, last_error,
        )
        # Wipe the partial todolist so the next retry sees a clean slate.
        try:
            todolist_path.unlink()
        except FileNotFoundError:
            pass

    reason = f"planner failed {config.limits.max_planner_attempts} times: {last_error}"
    state.mark_exhausted(reason)
    state.save(ws)
    return LoopResult(ExitCode.ERROR, reason)


def _extract_dev_id_from_reason(reason: str) -> str | None:
    """Pull a ``T-xxx`` id out of a PM reason string.

    PM's rule 1 emits ``"ready_for_qa T-007 has no matching qa item"``; we
    parse out the id so the scheduler can attach a dynamic qa.
    """
    if not reason:
        return None
    import re
    m = re.search(r"\b(T-\d+)\b", reason)
    return m.group(1) if m else None


def _attempt_log_digest(item: Item) -> str:
    import hashlib
    import json
    data = [(x.cycle, x.result, x.notes.strip()) for x in item.attempt_log]
    return hashlib.sha256(json.dumps(data).encode("utf-8")).hexdigest()


def _decision_advanced(before: Todolist, after: Todolist, decision: Decision) -> bool:
    """Return True iff the dispatched cycle actually moved the state forward.

    "Forward" means one of:
    * the assigned item reached a terminal result (dev → ready_for_qa; qa → done)
    * its attempt_log grew (a failed attempt is still forward progress — fuse
      will eventually catch it)
    * for qa decisions, the reviewed dev's status changed

    Silent failure — agent exited cleanly with no attempt_log entry — returns
    False; caller falls through to :func:`_reconcile`.
    """
    if decision.next not in {"dev", "qa"}:
        return True
    if decision.item_id is None:
        return True
    a = after.by_id(decision.item_id)
    if a is None:
        return False
    b = before.by_id(decision.item_id)
    if b is None:
        return True

    if decision.next == "dev":
        if a.status == "ready_for_qa" and b.status != "ready_for_qa":
            return True
        if _attempt_log_digest(a) != _attempt_log_digest(b):
            return True
        return False

    # qa
    if a.status == "done" and b.status != "done":
        return True
    if _attempt_log_digest(a) != _attempt_log_digest(b):
        return True
    # Aggregated qa can review multiple dev items — any one advancing counts.
    for reviewed in _reviewed_dev_ids(a, before):
        rb = before.by_id(reviewed)
        ra = after.by_id(reviewed)
        if rb is None or ra is None:
            continue
        if rb.status != ra.status:
            return True
        if _attempt_log_digest(rb) != _attempt_log_digest(ra):
            return True
    return False


def _classify_run_failure(result: RunResult) -> str:
    errors = " ".join(result.errors or [])
    if "stalled" in errors.lower():
        return "reconcile: agent stalled"
    if "exit code" in errors.lower() or "exited with" in errors.lower():
        return "reconcile: agent exited with error"
    if result.success:
        return "reconcile: agent exited cleanly, status not advanced"
    return "reconcile: agent failed"


def _reconcile(ws: WorkspacePaths, tl: Todolist, decision: Decision, result: RunResult, cycle: int) -> None:
    """Normalize an un-advanced dispatched item and append a pending attempt.

    Called after validator has run (so any rollback is already applied). The
    cascade of behaviors:

    * If the target is still ``doing`` → force ``pending`` (never leave
      doing across cycles; Inv-D).
    * Append a pending attempt with a classified reason. Idempotent on tail
      match so a post-crash re-run doesn't duplicate.
    """
    if decision.item_id is None:
        return
    item = tl.by_id(decision.item_id)
    if item is None:
        return
    note = _classify_run_failure(result)

    mutated = False
    if item.status == "doing":
        item.status = "pending"
        mutated = True
    if sw.append_scheduler_attempt(item, cycle, "pending", note):
        mutated = True
    if mutated:
        sw.scheduler_write(ws, tl)


def _fingerprint(tl: Todolist) -> str:
    """Structural digest of the todolist for stuck-detection.

    Granularity ``(id, status, len(attempt_log))`` — so an in-flight retry
    that pushes an item from N → N+1 attempts advances the fingerprint and
    resets the stuck counter, while a truly dead loop where nothing is being
    appended stays fingerprint-stable.
    """
    import hashlib
    parts = [f"{it.id}:{it.status}:{len(it.attempt_log)}" for it in tl.items]
    blob = "|".join(sorted(parts)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _fingerprint_stuck(history: list[str], threshold: int) -> bool:
    """True when the last ``threshold`` fingerprints are all identical.

    We need at least ``threshold`` samples before tripping; a shorter history
    always returns False.
    """
    if threshold <= 1:
        return False
    if len(history) < threshold:
        return False
    tail = history[-threshold:]
    return len(set(tail)) == 1


def _item_failures(item: Item) -> int:
    """Number of failed (pending-result) attempts in an item's attempt_log."""
    return sum(1 for a in item.attempt_log if a.result == "pending")


def _fuse(ws: WorkspacePaths | Path, tl: Todolist, max_attempts: int, cycle: int) -> list[tuple[str, str]]:
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


def _cascade(
    ws: WorkspacePaths | Path, tl: Todolist, cycle: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Cascade ``abandoned`` through downstream deps; downgrade stranded qa-dev.

    Fixed-point iteration: anything whose dep is abandoned becomes abandoned
    itself. Independent DAG branches survive. After propagation, any qa item
    already in ``abandoned`` causes its paired ``ready_for_qa`` dev to roll
    back to ``pending`` (give retry-and-fuse another shot before final fuse).

    Returns ``(newly_abandoned, downgraded)`` where each element is a
    ``(item_id, reason)`` tuple. Both lists are needed by the main loop so
    that downgrade-only passes still trigger ``scheduler_write`` — without
    persisting, in-memory state drifts from disk and subsequent dispatches
    see stale ``ready_for_qa`` on the file system.
    """
    newly: list[tuple[str, str]] = []
    downgraded: list[tuple[str, str]] = []
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

    # qa abandoned → downgrade every reviewed dev still at ready_for_qa.
    # Aggregated qa reviews multiple devs; all of them need another shot.
    for it in tl.items:
        if it.type != "qa" or it.status != "abandoned":
            continue
        for dev_id in _reviewed_dev_ids(it, tl):
            dev = tl.by_id(dev_id)
            if dev is None or dev.status != "ready_for_qa":
                continue
            reason = f"qa {it.id} abandoned"
            if sw.downgrade_reviewed_dev(tl, dev_id, cycle, reason):
                downgraded.append((dev_id, reason))
    return newly, downgraded


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


def _wipe_agentloop_state(ws: WorkspacePaths) -> None:
    """Wipe the current workspace's run state, preserving its config.toml.

    ``--fresh`` resets this workspace's todolist, state.json, and runs logs
    without erasing its per-workspace ``config.toml`` (seeded by the CLI /
    manager before the run), its ``design.md`` link (pointing at the real
    spec the user passed in), nor any sibling workspace under
    ``<cwd>/.agentloop/workspaces/``.
    """
    ws_dir = ws.workspace_dir
    if not ws_dir.exists():
        ws_dir.mkdir(parents=True, exist_ok=True)
        return
    # Preserve config.toml and design.md across the wipe — both are put in
    # place by the caller just before the run starts, and naively rmtree-ing
    # the whole workspace would drop them (breaking per-workspace config
    # overrides and making the spec unreachable from the subprocess cwd).
    preserve = {ws.config_file.name, ws.design.name}
    for child in ws_dir.iterdir():
        if child.name in preserve:
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
