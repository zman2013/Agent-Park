"""Code-version PM agent.

Implements the deterministic decision table from DESIGN §12:

    1. If any dev item is ready_for_qa → dispatch the matching qa item.
    2. If any dev item is pending and all deps are done → dispatch dev.
    3. If all items are done → "done".
    4. Otherwise → "done" with reason="no actionable items".

The PM never modifies the todolist; it only produces a Decision.
"""
from __future__ import annotations

from ..state import Decision
from ..todolist import Item, Todolist


def decide(todolist: Todolist) -> Decision:
    items = todolist.items

    # 1. ready_for_qa → qa
    for dev in items:
        if dev.type == "dev" and dev.status == "ready_for_qa":
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

    # 2. pending dev with all deps done → dev
    for dev in items:
        if dev.type != "dev" or dev.status != "pending":
            continue
        if not _deps_done(dev, items):
            continue
        return Decision(
            next="dev",
            item_id=dev.id,
            reason=f"dev {dev.id} — deps satisfied",
        )

    # 3. all done
    if items and all(it.status == "done" for it in items):
        return Decision(next="done", item_id=None, reason="all items done")

    # 4. fallback
    return Decision(next="done", item_id=None, reason="no actionable items")


def _find_qa_for(dev_id: str, items: list[Item]) -> Item | None:
    for it in items:
        if it.type != "qa" or it.status == "done":
            continue
        source = (it.source or "").lower()
        if dev_id.lower() in source:
            return it
    return None


def _deps_done(item: Item, items: list[Item]) -> bool:
    index = {it.id: it for it in items}
    for dep in item.dependencies:
        dep_item = index.get(dep)
        if dep_item is None or dep_item.status != "done":
            return False
    return True
