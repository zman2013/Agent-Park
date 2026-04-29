"""Validator — enforces the state-machine rules from DESIGN §5.

Called with the todolist before and after an agent run. Raises
``ValidationError`` when the transition violates the permission matrix.
"""
from __future__ import annotations

from .todolist import Item, Todolist, VALID_STATUSES, VALID_TYPES


class ValidationError(Exception):
    pass


# Allowed (from_status, to_status) transitions keyed by actor.
# planner is handled separately (only legal as the initial write).
_ALLOWED_DEV = {
    ("pending", "doing"),
    ("doing", "ready_for_qa"),
    ("doing", "pending"),
    # 文件契约下 LLM 一次写入即完成，`doing` 中间态无法可靠观察，
    # 允许 pending 直跳 ready_for_qa / 保持 pending（失败）。
    ("pending", "ready_for_qa"),
    ("pending", "pending"),
}

_ALLOWED_QA_SELF = {
    # the qa item itself transitions pending -> done
    ("pending", "done"),
}
_ALLOWED_QA_REVIEWED = {
    # the dev item being reviewed transitions ready_for_qa -> done/pending
    ("ready_for_qa", "done"),
    ("ready_for_qa", "pending"),
}


def validate_transition(
    before: Todolist,
    after: Todolist,
    actor: str,
    item_id: str | None,
) -> None:
    """Raise ``ValidationError`` if the diff between ``before`` and ``after``
    violates the permission matrix for ``actor``.

    Notes
    -----
    * ``planner`` is only legal when ``before`` has zero items.
    * ``dev`` may modify exactly one existing item (its assigned ``item_id``).
    * ``qa`` may modify the reviewed dev item and the qa item itself, and may
      append new items.
    * No actor may modify a ``done`` item's status.
    """
    _check_item_shape(after)

    if actor == "scheduler":
        # Scheduler writes (fuse/cascade/reconcile/dynamic-qa/stale-doing) are
        # the loop's own bookkeeping. The transition matrix for dev/qa does
        # not cover them (e.g. pending → abandoned, ready_for_qa → pending
        # via cascade), but they are still structurally well-formed — shape
        # check above is sufficient.
        return

    if actor == "planner":
        _validate_planner(before, after)
        return

    if actor == "dev":
        _validate_dev(before, after, item_id)
        return

    if actor == "qa":
        _validate_qa(before, after, item_id)
        return

    raise ValidationError(f"unknown actor: {actor}")


# ----- shape checks -------------------------------------------------------


def _check_item_shape(tl: Todolist) -> None:
    seen: set[str] = set()
    for it in tl.items:
        if it.id in seen:
            raise ValidationError(f"duplicate item id: {it.id}")
        seen.add(it.id)
        if it.type not in VALID_TYPES:
            raise ValidationError(f"item {it.id}: invalid type {it.type!r}")
        if it.status not in VALID_STATUSES:
            raise ValidationError(f"item {it.id}: invalid status {it.status!r}")


# ----- planner ------------------------------------------------------------


def _validate_planner(before: Todolist, after: Todolist) -> None:
    if before.items:
        raise ValidationError("planner cannot run when todolist already has items")
    if not after.items:
        raise ValidationError("planner must produce at least one item")
    for it in after.items:
        if it.status == "done":
            raise ValidationError(
                f"planner produced item {it.id} in status 'done' — not allowed"
            )


# ----- dev ----------------------------------------------------------------


def _validate_dev(before: Todolist, after: Todolist, item_id: str | None) -> None:
    if item_id is None:
        raise ValidationError("dev requires an item_id")

    before_ids = {it.id for it in before.items}
    after_ids = {it.id for it in after.items}

    if after_ids - before_ids:
        added = sorted(after_ids - before_ids)
        raise ValidationError(f"dev cannot add items, but added: {added}")
    if before_ids - after_ids:
        removed = sorted(before_ids - after_ids)
        raise ValidationError(f"dev cannot remove items, but removed: {removed}")

    for item in after.items:
        b = before.by_id(item.id)
        if b is None:
            continue
        if _item_equal(b, item):
            continue
        if item.id != item_id:
            raise ValidationError(
                f"dev modified item {item.id}, but was assigned {item_id}"
            )
        if b.status == "done":
            raise ValidationError(f"dev modified done item {item.id}")
        if b.type != "dev":
            raise ValidationError(f"dev modified non-dev item {item.id}")
        if (b.status, item.status) not in _ALLOWED_DEV:
            raise ValidationError(
                f"dev: illegal status transition on {item.id}: "
                f"{b.status} -> {item.status}"
            )


# ----- qa -----------------------------------------------------------------


def _validate_qa(before: Todolist, after: Todolist, item_id: str | None) -> None:
    if item_id is None:
        raise ValidationError("qa requires an item_id (the qa item)")

    qa_self = before.by_id(item_id)
    if qa_self is None:
        raise ValidationError(f"qa item {item_id} does not exist before the run")
    if qa_self.type != "qa":
        raise ValidationError(f"qa item {item_id} is not type=qa")
    if qa_self.status == "done":
        raise ValidationError(f"qa item {item_id} already done")

    reviewed_id = _reviewed_dev_id(qa_self, before)

    before_ids = {it.id for it in before.items}
    after_ids = {it.id for it in after.items}

    # removals are forbidden
    removed = before_ids - after_ids
    if removed:
        raise ValidationError(f"qa cannot remove items, but removed: {sorted(removed)}")

    # status changes must be on {qa_self, reviewed_id}; any other change is illegal
    allowed_touch = {item_id}
    if reviewed_id:
        allowed_touch.add(reviewed_id)

    for item in after.items:
        b = before.by_id(item.id)
        if b is None:
            # new item appended by qa (findings → dev-fix item)
            if item.status == "done":
                raise ValidationError(
                    f"qa appended item {item.id} with status 'done' — not allowed"
                )
            continue
        if _item_equal(b, item):
            continue
        if item.id not in allowed_touch:
            raise ValidationError(
                f"qa modified unrelated item {item.id} (expected changes only on "
                f"{sorted(allowed_touch)})"
            )
        if b.status == "done":
            raise ValidationError(f"qa modified done item {item.id}")
        if item.id == item_id:
            # qa item itself
            if (b.status, item.status) not in _ALLOWED_QA_SELF and b.status != item.status:
                raise ValidationError(
                    f"qa: illegal qa-self transition on {item.id}: "
                    f"{b.status} -> {item.status}"
                )
        else:
            # the dev item under review
            if b.type != "dev":
                raise ValidationError(
                    f"qa modified non-dev item {item.id} (type={b.type})"
                )
            if (b.status, item.status) not in _ALLOWED_QA_REVIEWED and b.status != item.status:
                raise ValidationError(
                    f"qa: illegal reviewed transition on {item.id}: "
                    f"{b.status} -> {item.status}"
                )


def _reviewed_dev_id(qa_item: Item, before: Todolist) -> str | None:
    """Return the dev item id this qa item targets.

    Uses ``source: follows T-xxx`` or ``source: qa-finding of T-xxx``; falls
    back to the first ``ready_for_qa`` dev item in the todolist. Matching is
    case-insensitive because PM/planner prompts accept ``t-002`` as well as
    ``T-002``; treating them differently here causes spurious
    "modified unrelated item" rollbacks when the QA agent echoes the
    lowercased form.
    """
    source = qa_item.source or ""
    for token in source.split():
        stripped = token.strip(".,;")
        if stripped[:2].upper() == "T-" and len(stripped) > 2:
            # Normalize to uppercase so downstream id comparisons (which run
            # against canonical uppercase ids in the todolist) succeed.
            return "T-" + stripped[2:]
    for it in before.items:
        if it.type == "dev" and it.status == "ready_for_qa":
            return it.id
    return None


# ----- equality helpers ---------------------------------------------------


def _item_equal(a: Item, b: Item) -> bool:
    return (
        a.id == b.id
        and a.type == b.type
        and a.status == b.status
        and a.title == b.title
        and a.dependencies == b.dependencies
        and a.source == b.source
        and a.dev_notes == b.dev_notes
        and a.findings == b.findings
        and [(x.cycle, x.result, x.notes) for x in a.attempt_log]
        == [(x.cycle, x.result, x.notes) for x in b.attempt_log]
    )
