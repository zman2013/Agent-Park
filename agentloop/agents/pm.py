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

    # 1. ready_for_qa → qa
    for dev in items:
        if dev.type != "dev":
            continue
        if dev.status != "ready_for_qa":
            continue
        qa = _find_qa_for(dev.id, items)
        if qa is not None:
            return Decision(
                next="qa",
                item_id=qa.id,
                reason=f"qa {qa.id} → review {dev.id}",
            )
        return Decision(
            next="qa",
            item_id=None,
            reason=f"ready_for_qa {dev.id} has no matching qa item",
        )

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


def _find_qa_for(dev_id: str, items: list[Item]) -> Item | None:
    for it in items:
        if it.type != "qa":
            continue
        if it.status in _TERMINAL:
            continue
        source = (it.source or "").lower()
        if dev_id.lower() in source:
            return it
    return None


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
