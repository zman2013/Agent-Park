"""Code-version PM agent.

Implements the deterministic decision table from DESIGN §12 (v2 adds
``abandoned`` as a terminal state, treated like ``done`` for scheduling
purposes but distinguishable in the summary):

    1. If any dev item is ready_for_qa (and not abandoned) → dispatch the
       matching qa item. If no matching qa item exists, emit item_id=None
       so the loop layer can create a dynamic qa.
    2. If any dev item is pending and all deps are done/abandoned → dispatch
       dev. (A dep in ``abandoned`` status blocks the downstream cascade
       elsewhere; PM itself just needs a non-in-flight dep.)
    3. If all items are in {done, abandoned} → "done".
    4. Otherwise → "done" with reason="no actionable items".

The PM never modifies the todolist; it only produces a Decision.
"""
from __future__ import annotations

from ..state import Decision
from ..todolist import Item, Todolist


_TERMINAL = {"done", "abandoned"}


def decide(todolist: Todolist) -> Decision:
    items = todolist.items

    # 1. ready_for_qa → qa, but ONLY when a qa with all-terminal deps exists.
    #    If every covering qa still has unmet deps, fall through to rule 2 so
    #    PM advances the actually-pending dep instead of redispatching a qa
    #    that would just self-fail on "dependencies not complete".
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
        # Remember the first ready_for_qa dev that had no ready qa. If rule
        # 2 finds no actionable dev either, we'll come back and either pick
        # any covering qa (fallback) or emit a dynamic-qa marker.
        if deferred_dev is None:
            deferred_dev = dev

    # 2. pending dev with all deps terminal (done or abandoned) → dev
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

    # 1b. No actionable dev — fall back to the deferred dev's best qa, even
    #     if its deps aren't all terminal. This preserves dynamic-qa creation
    #     for stranded ready_for_qa devs and lets the loop's qa-demote /
    #     cascade machinery surface the deadlock instead of stalling here.
    if deferred_dev is not None:
        qa = _find_qa_for(deferred_dev.id, items, require_ready=False)
        if qa is not None:
            return Decision(
                next="qa",
                item_id=qa.id,
                reason=f"qa {qa.id} → review {deferred_dev.id} (fallback, deps unmet)",
            )
        return Decision(
            next="qa",
            item_id=None,
            reason=f"ready_for_qa {deferred_dev.id} has no matching qa item",
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
    with the fewest still-pending non-dev blockers; ties broken by depth
    (shorter chain first), then file order.

    ``require_ready=True`` filters to qa with zero unmet deps (PM rule-1
    primary path). ``require_ready=False`` allows any covering qa as a
    fallback when no actionable dev is left to advance.
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
            elif dep.status not in _TERMINAL:
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
    """qa covers dev_id iff dev_id is reachable from qa.dependencies (DFS upward).

    Fallback: if the qa has no dependencies declared at all, fall back to
    matching ``dev_id`` against the source string. This preserves the
    historical planner contract where source="follows T-xxx" was the only
    pairing signal — switching to dep-DAG strict matching would silently
    drop those items.
    """
    if qa.dependencies:
        seen: set[str] = set()
        stack: list[str] = list(qa.dependencies)
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            if x == dev_id:
                return True
            nxt = index.get(x)
            if nxt is not None:
                stack.extend(nxt.dependencies)
        return False
    # No deps → fall back to source-text matching (legacy contract).
    return dev_id.lower() in (qa.source or "").lower()


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
        # Gate on terminal states; an abandoned dep lets scheduling continue
        # (cascade will have run first, so an independent branch of the DAG
        # should not be blocked by an unrelated failure).
        if dep_item.status not in _TERMINAL:
            return False
    return True
