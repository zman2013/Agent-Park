"""Scheduler write path for todolist mutations outside dev/qa agents.

The state-machine v2 design requires the loop itself to mutate the todolist in
a few situations that don't belong to any LLM agent:

* Fuse a per-item failure into ``abandoned`` once attempt_log crosses a limit.
* Cascade ``abandoned`` through downstream dependencies, and downgrade a dev
  item whose paired qa is abandoned back to ``pending``.
* Reset a ``doing`` item left over from a crashed dev subprocess.
* Inject a dynamic qa item when PM spots a stranded ``ready_for_qa`` dev.
* Detect dependency cycles / dangling references at boot.

All writes funnel through :func:`scheduler_write` which parses, validates via
the ``actor="scheduler"`` bypass, and persists. Mutations are written to the
in-memory Todolist by the individual helpers so callers can chain them before
persisting.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .todolist import (
    Attempt,
    Item,
    Todolist,
    parse as parse_todolist,
    write as write_todolist,
)
from .validator import validate_transition


SCHEDULER = "scheduler"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- persistence --------------------------------------------------------


def scheduler_write(cwd: Path, tl: Todolist) -> None:
    """Persist ``tl`` to disk via the same renderer as agents use.

    Runs the scheduler-actor validator first so structural corruption (duplicate
    ids, invalid type/status) never reaches disk. The scheduler bypass skips the
    per-actor transition matrix but still enforces ``_check_item_shape``, which
    is exactly the guardrail this path needs. ``before`` is read from the file
    to match what agents see; if that read fails we still fall through to the
    shape-only check by passing an empty Todolist.
    """
    try:
        before = parse_todolist(cwd)
    except Exception:  # noqa: BLE001 — treat unreadable file as "no prior state"
        before = Todolist(metadata={}, items=[])
    validate_transition(before, tl, SCHEDULER, None)
    write_todolist(cwd, tl)


# ----- single-item mutators ----------------------------------------------


def mark_abandoned(tl: Todolist, item_id: str, reason: str, cycle: int) -> bool:
    """Set item ``item_id`` to ``abandoned`` and append an attempt_log entry.

    Returns True if the item was found and mutated, False otherwise (id missing
    or already in a terminal state). Reason is stored as an attempt note so the
    rendered todolist carries the abandon reason verbatim.
    """
    item = tl.by_id(item_id)
    if item is None:
        return False
    if item.status in {"done", "abandoned"}:
        return False
    item.status = "abandoned"
    item.attempt_log.append(
        Attempt(cycle=cycle, result="pending", notes=f"abandoned: {reason}")
    )
    return True


def downgrade_reviewed_dev(
    tl: Todolist, dev_id: str, cycle: int, reason: str
) -> bool:
    """Roll a ``ready_for_qa`` dev item back to ``pending``.

    Used when its paired qa ended up abandoned and we want to give dev another
    shot through retry-and-fuse rather than cascading the abandon.
    """
    item = tl.by_id(dev_id)
    if item is None:
        return False
    if item.type != "dev":
        return False
    if item.status != "ready_for_qa":
        return False
    item.status = "pending"
    item.attempt_log.append(
        Attempt(cycle=cycle, result="pending", notes=f"downgraded: {reason}")
    )
    return True


def append_scheduler_attempt(
    item: Item, cycle: int, result: str, notes: str
) -> bool:
    """Append an attempt_log entry to ``item``, idempotent on (cycle, result, notes).

    Returns True if the entry was appended, False if a duplicate already sits
    at the tail of the log. Idempotency protects against a reconcile pass
    re-running on the same cycle after a crash.
    """
    tail = item.attempt_log[-1] if item.attempt_log else None
    if tail is not None and (tail.cycle, tail.result, tail.notes) == (
        cycle,
        result,
        notes,
    ):
        return False
    item.attempt_log.append(Attempt(cycle=cycle, result=result, notes=notes))
    return True


# ----- dynamic qa creation -----------------------------------------------


def create_dynamic_qa(tl: Todolist, dev_id: str, cycle: int) -> Item:
    """Append a new qa item that reviews ``dev_id``.

    Called when PM spots a ``ready_for_qa`` dev without any paired qa item
    (e.g. planner forgot it, or dev overshot and marked ready without the
    planner queueing a qa). The new id comes from :meth:`Todolist.next_id`,
    status is pending, deps reference the dev item.
    """
    new_id = tl.next_id()
    qa = Item(
        id=new_id,
        type="qa",
        status="pending",
        title=f"审查 {dev_id}",
        dependencies=[dev_id],
        source=f"follows {dev_id}",
        attempt_log=[
            Attempt(
                cycle=cycle,
                result="pending",
                notes=f"auto-created by scheduler for {dev_id}",
            )
        ],
    )
    tl.items.append(qa)
    return qa


# ----- startup health checks ---------------------------------------------


def stale_doing_reconcile(tl: Todolist, boot_cycle: int) -> list[str]:
    """Reset any item left in ``doing`` to ``pending``.

    A ``doing`` item that survives a process restart signals either an agent
    crash or a stall-kill. ``pending`` is the only recoverable state; the
    attempt_log gets a note so attempt_count still reflects the failed run.
    Returns the list of reset ids.
    """
    reset: list[str] = []
    for item in tl.items:
        if item.status != "doing":
            continue
        item.status = "pending"
        append_scheduler_attempt(
            item,
            cycle=boot_cycle,
            result="pending",
            notes="stale doing reset by scheduler on boot",
        )
        reset.append(item.id)
    return reset


def find_dep_cycles(tl: Todolist) -> list[list[str]]:
    """Return all SCCs of size ≥ 2 (or self-loops) among item dependencies.

    Uses Tarjan's algorithm. Missing dependencies are ignored here (see
    :func:`find_dangling_deps`). Each cycle is returned as the list of item
    ids participating, with stable (input) order.
    """
    index: dict[str, Item] = {it.id: it for it in tl.items}
    graph: dict[str, list[str]] = {
        it.id: [d for d in it.dependencies if d in index] for it in tl.items
    }

    idx_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    cycles: list[list[str]] = []

    def strongconnect(v: str) -> None:
        indices[v] = idx_counter[0]
        lowlink[v] = idx_counter[0]
        idx_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, []):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1 or (len(scc) == 1 and v in graph.get(v, [])):
                # preserve input order
                ordered = [it.id for it in tl.items if it.id in set(scc)]
                cycles.append(ordered)

    for node in graph:
        if node not in indices:
            strongconnect(node)
    return cycles


def find_dangling_deps(tl: Todolist) -> list[tuple[str, str]]:
    """Return ``(item_id, missing_dep)`` pairs for references to undefined ids."""
    ids = {it.id for it in tl.items}
    out: list[tuple[str, str]] = []
    for it in tl.items:
        for dep in it.dependencies:
            if dep not in ids:
                out.append((it.id, dep))
    return out
