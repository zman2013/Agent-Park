"""Code-version PM agent.

Implements the deterministic decision table from DESIGN §12 (v2 adds
``abandoned`` as a terminal state, treated like ``done`` for scheduling
purposes but distinguishable in the summary):

    1. If any dev item is ready_for_qa (and not abandoned) → dispatch the
       matching qa item. If no matching qa item exists, emit item_id=None
       so the loop layer can create a dynamic qa.
    2. If any dev item is pending and all deps are done/abandoned/
       ready_for_qa → dispatch dev. (A dep in ``abandoned`` status blocks
       the downstream cascade elsewhere; ``ready_for_qa`` qualifies because
       the dev artifact is already on disk — only the qa stamp is pending,
       and if qa later rolls the dep back to pending the next dispatch
       replays. PM itself just needs a non-in-flight dep.)
    3. If all items are in {done, abandoned} → "done".
    4. Otherwise → "done" with reason="no actionable items".

The PM never modifies the todolist; it only produces a Decision.
"""
from __future__ import annotations

from ..state import Decision
from ..todolist import Item, Todolist


_TERMINAL = {"done", "abandoned"}
# Statuses that count as "dep produced its artifact" for downstream dev
# scheduling. ``ready_for_qa`` qualifies because the dev's output is already
# on disk — qa hasn't stamped it yet, but a downstream dev can read it. If
# qa later rolls the dep back to ``pending``, cascade/replay will rerun
# downstream.
_DEP_READY = _TERMINAL | {"ready_for_qa"}


def decide(todolist: Todolist) -> Decision:
    items = todolist.items

    # 1. ready_for_qa → qa, but ONLY when a covering qa has all sibling
    #    deps in {done, abandoned, ready_for_qa}. If every covering qa
    #    still has a sibling dep that's pending/doing, fall through to
    #    rule 2 so PM advances the actually-pending dep instead of
    #    redispatching a qa that would just self-fail on "dependencies
    #    not complete".
    deferred_dev: Item | None = None
    for dev in items:
        if dev.type != "dev":
            continue
        if dev.status != "ready_for_qa":
            continue
        qa = _find_qa_for(dev.id, items, require_ready=True)
        if qa is not None:
            return Decision(
                next="qa",
                item_id=qa.id,
                reason=f"qa {qa.id} → review {dev.id}",
            )
        # Remember the first ready_for_qa dev that had no ready qa. If
        # rule 2 also finds no actionable dev, rule 1b emits a dynamic-qa
        # marker so the loop can create a 1:1 qa scoped to this dev.
        if deferred_dev is None:
            deferred_dev = dev

    # 2. pending dev with all deps in _DEP_READY → dev
    for dev in items:
        if dev.type != "dev" or dev.status != "pending":
            continue
        if not _deps_ok(dev, items):
            continue
        return Decision(
            next="dev",
            item_id=dev.id,
            reason=f"dev {dev.id} — deps satisfied",
        )

    # 1b. No actionable dev — there's a stranded ready_for_qa dev whose only
    #     covering qa items still have unmet sibling deps. We do NOT
    #     re-dispatch one of those covering qa items: that just causes a
    #     "dependencies not complete" self-fail and (after demote-blocked-qa)
    #     a downgrade of the very dev that's already ready_for_qa, throwing
    #     away its work. Instead emit ``item_id=None`` so the loop creates a
    #     dynamic 1:1 qa scoped to ``deferred_dev`` only — its review
    #     stamps the dev without requiring siblings to be done.
    if deferred_dev is not None:
        return Decision(
            next="qa",
            item_id=None,
            reason=(
                f"ready_for_qa {deferred_dev.id}: covering qa item(s) have "
                f"unmet sibling deps; create dynamic 1:1 qa"
            ),
        )

    # 3. all terminal
    if items and all(it.status in _TERMINAL for it in items):
        abandoned = sum(1 for it in items if it.status == "abandoned")
        if abandoned:
            return Decision(
                next="done",
                item_id=None,
                reason=f"{abandoned} item(s) abandoned, rest done",
            )
        return Decision(next="done", item_id=None, reason="all items done")

    # 4. fallback
    return Decision(next="done", item_id=None, reason="no actionable items")


def _find_qa_for(
    dev_id: str, items: list[Item], *, require_ready: bool = False
) -> Item | None:
    """Pick the qa whose ready-to-run distance from dev is minimum.

    An aggregated terminal qa may textually mention dev_id in its source,
    but its other deps may still be pending — dispatching it loops the
    scheduler (qa self-fails on "dependencies not complete"). We instead
    walk the dependency DAG: a qa "covers" dev_id iff dev_id is reachable
    from the qa via dependencies. Among covering qa items, pick the one
    with the fewest still-blocking siblings; ties broken by depth
    (shorter chain first), then file order.

    ``require_ready=True`` filters to qa with zero blocking siblings (PM
    rule-1 primary path; this is the only path the scheduler uses today).
    ``require_ready=False`` is kept for callers that want the best-effort
    pick regardless of readiness.
    """
    index = {it.id: it for it in items}
    candidates: list[tuple[tuple[int, int], int, Item]] = []

    for idx, it in enumerate(items):
        if it.type != "qa" or it.status in _TERMINAL:
            continue
        if not _qa_covers_dev(it, dev_id, index):
            continue
        pending_blockers = 0
        for d in it.dependencies:
            if d == dev_id:
                continue
            dep = index.get(d)
            if dep is None:
                # Dangling dep — never resolves; count as blocker so this qa
                # falls out of the require_ready=True path.
                pending_blockers += 1
            elif dep.status not in _DEP_READY:
                # ``ready_for_qa`` sibling counts as ready: its artifact is
                # on disk and an aggregated qa can review both this dev and
                # that sibling in one pass. Only ``pending``/``doing`` are
                # real blockers.
                pending_blockers += 1
        if require_ready and pending_blockers > 0:
            continue
        depth = _shortest_dep_depth(it.id, dev_id, index)
        candidates.append(((pending_blockers, depth), idx, it))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2]


def _qa_covers_dev(qa: Item, dev_id: str, index: dict[str, Item]) -> bool:
    """qa covers dev_id iff the validator would let this qa review dev_id.

    Coverage MUST stay aligned with ``validator._reviewed_dev_ids``: if PM
    dispatches a qa for dev_id but the validator's source-derived reviewed
    set excludes dev_id, the qa run will be rolled back and the loop
    spins. We therefore reuse the same source-token parsing here instead of
    walking the dep DAG (a transitive dep is "reachable" but not
    "reviewed").
    """
    import re

    raw = re.findall(r"[Tt]-\d+", qa.source or "")
    source_ids = {"T-" + r[2:] for r in raw}
    if source_ids:
        return dev_id in source_ids
    # No T-xxx tokens in source: validator falls back to "first ready_for_qa
    # dev". Mirror that here so PM and validator agree.
    for it in index.values():
        if it.type == "dev" and it.status == "ready_for_qa":
            return it.id == dev_id
    return False


def _shortest_dep_depth(qa_id: str, dev_id: str, index: dict[str, Item]) -> int:
    """BFS from qa to dev along dependencies; smaller depth = closer qa."""
    from collections import deque

    q: deque[tuple[str, int]] = deque([(qa_id, 0)])
    seen = {qa_id}
    while q:
        node, d = q.popleft()
        if node == dev_id:
            return d
        cur = index.get(node)
        if cur is None:
            continue
        for dep in cur.dependencies:
            if dep not in seen:
                seen.add(dep)
                q.append((dep, d + 1))
    return 1 << 30


def _deps_ok(item: Item, items: list[Item]) -> bool:
    index = {it.id: it for it in items}
    for dep in item.dependencies:
        dep_item = index.get(dep)
        if dep_item is None:
            return False
        # Gate on "dep produced its artifact": done, abandoned (cascade has
        # run; independent branches survive), or ready_for_qa (artifact on
        # disk, qa pending). Anything else (pending, doing) is in-flight.
        if dep_item.status not in _DEP_READY:
            return False
    return True
