"""Human plan-review gate.

Sits between phase 0 (planner) and phase 1 (the dev/qa loop). When enabled,
the loop writes ``plan-review.json`` after the planner produces a todolist and
**exits** with :class:`ExitCode.AWAITING_REVIEW` rather than blocking on stdin.

Why a file gate and not ``input()``
-----------------------------------
The pre-existing ``--review-plan`` flag called ``input()``, which is dead in
the only path that actually matters: ``agentloop_manager.start`` spawns the
loop with ``stdin=subprocess.DEVNULL``, so the prompt raised ``EOFError`` and
was swallowed. Blocking also pins a process in the foreground for as long as
the human takes to review — hours, if the review happens the next morning.

A file gate has neither problem: the loop exits cleanly, the workspace holds
all the state needed to resume, and approval can arrive from the UI or the CLI.
(The Feishu card is notification-only — it pushes the review request and points
the reviewer at the UI; there is no inbound card action.)

Digest binding
--------------
``todolist_digest`` records the todolist content that was actually approved.
Editing the plan in the UI and then approving is the primary "reject" path
(cheaper than re-running the planner), so approval must bind to the *edited*
bytes. On resume the loop recomputes the digest: a mismatch means the file
changed after approval, and the gate reverts to ``awaiting`` rather than
executing a plan no human ever saw.

State machine::

    (no file) ──plan written──▶ awaiting ──approve──▶ approved ──resume──▶ consumed
                                   ▲                                          │
                                   └──── digest mismatch ─────────────────────┘
                                   │
                                   └──reject──▶ rejected ──(--fresh)──▶ (no file)
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .todolist import Todolist
from .workspace import WorkspacePaths

PLAN_REVIEW_FILE = "plan-review.json"
REJECTION_NOTE_FILE = "plan-rejection.md"

# Gate states
AWAITING = "awaiting"
APPROVED = "approved"
REJECTED = "rejected"
CONSUMED = "consumed"

VALID_STATES = {AWAITING, APPROVED, REJECTED, CONSUMED}

# config.toml `[review] plan = ...` policies
POLICY_ALWAYS = "always"
POLICY_NEVER = "never"
POLICY_WHEN_UNVERIFIED = "when_unverified"

VALID_POLICIES = {POLICY_ALWAYS, POLICY_NEVER, POLICY_WHEN_UNVERIFIED}


def review_path(ws: WorkspacePaths) -> Path:
    return ws.workspace_dir / PLAN_REVIEW_FILE


def rejection_note_path(ws: WorkspacePaths) -> Path:
    return ws.workspace_dir / REJECTION_NOTE_FILE


def todolist_digest(ws: WorkspacePaths) -> str:
    """SHA-256 of the raw todolist bytes, or "" when the file is absent.

    Deliberately hashes raw bytes rather than the parsed model: a human editing
    the plan in the UI may change a title, a dependency, or drop an item, and
    every one of those must invalidate a prior approval. Parsing first would
    silently normalize away differences the reviewer intended.
    """
    path = ws.todolist
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class PlanReview:
    state: str = AWAITING
    planned_at: str = ""
    todolist_digest: str = ""
    reviewed_at: str | None = None
    note: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    # Set once a review request has been pushed for the current awaiting
    # episode. Relaunching a loop that is still awaiting must not re-spam the
    # reviewer; cleared whenever the gate re-opens (drift-triggered revert).
    notified_at: str | None = None

    # ----- persistence ---------------------------------------------------

    @classmethod
    def load(cls, ws: WorkspacePaths) -> "PlanReview | None":
        """Read the gate file. Returns ``None`` when absent or unreadable.

        Callers that make a *safety* decision must not conflate the two:
        ``check_gate`` pairs this with :func:`gate_file_present` so a corrupt
        file keeps the loop waiting instead of being read as "no gate".
        """
        path = review_path(ws)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        state = str(data.get("state") or "")
        if state not in VALID_STATES:
            return None
        stats = data.get("stats") or {}
        if not isinstance(stats, dict):
            # A hand-edited or older-schema gate whose `stats` isn't a mapping
            # must read as unreadable, not raise out of load() — every caller
            # (check_gate, the CLI, the notifier) expects None on bad input and
            # would otherwise crash instead of failing closed.
            return None
        return cls(
            state=state,
            planned_at=str(data.get("planned_at") or ""),
            todolist_digest=str(data.get("todolist_digest") or ""),
            reviewed_at=data.get("reviewed_at"),
            note=data.get("note"),
            stats=dict(stats),
            notified_at=data.get("notified_at"),
        )

    def save(self, ws: WorkspacePaths) -> None:
        path = review_path(ws)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": self.state,
            "planned_at": self.planned_at,
            "todolist_digest": self.todolist_digest,
            "reviewed_at": self.reviewed_at,
            "note": self.note,
            "stats": self.stats,
            "notified_at": self.notified_at,
        }
        # Atomic: write a sibling temp file then rename. A crash mid-write must
        # never leave a truncated gate file behind — a half-written file reads
        # as unparseable, and `check_gate` then has to keep the loop waiting
        # (fail closed) even though the plan may already be approved.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(path)


def gate_file_present(ws: WorkspacePaths) -> bool:
    """Whether a gate file exists on disk, regardless of whether it parses.

    ``PlanReview.load`` returns ``None`` both for "no gate was ever opened"
    and "the gate is there but corrupt". Only the first may proceed into
    phase 1; this separates them.
    """
    return review_path(ws).exists()


def summarize(todolist: Todolist) -> dict[str, Any]:
    """Counts shown to the reviewer in the UI / Feishu card.

    ``unverified`` counts dev items with no ``checks`` declared. The field does
    not exist yet (it arrives with the evidence gate); until then the count is
    simply every dev item, which is the honest answer — nothing is machine
    verifiable today.
    """
    dev = [it for it in todolist.items if it.type == "dev"]
    qa = [it for it in todolist.items if it.type == "qa"]
    unverified = [it for it in dev if not getattr(it, "checks", None)]
    return {
        "items": len(todolist.items),
        "dev": len(dev),
        "qa": len(qa),
        "unverified": len(unverified),
        "unverified_ids": [it.id for it in unverified],
    }


def open_gate(ws: WorkspacePaths, todolist: Todolist) -> PlanReview:
    """Write a fresh ``awaiting`` gate bound to the current todolist."""
    review = PlanReview(
        state=AWAITING,
        planned_at=_utcnow(),
        todolist_digest=todolist_digest(ws),
        stats=summarize(todolist),
    )
    review.save(ws)
    return review


def approve(ws: WorkspacePaths, *, note: str | None = None) -> PlanReview:
    """Mark the gate approved, binding to the todolist as it exists *now*.

    Re-digesting at approval time (rather than trusting the digest written when
    the gate opened) is what makes "edit the plan in the UI, then approve" work
    — the reviewer's edits become the approved plan.

    The plan is validated first. Approving an empty or structurally invalid
    todolist is worse than an error: the file exists, so the next run skips the
    planner, consumes the approval, PM immediately returns ``done``, and the
    loop reports SUCCESS having executed nothing. Same invariant the planner's
    output must satisfy — at least one parseable item.
    """
    review = PlanReview.load(ws)
    if review is None:
        if gate_file_present(ws):
            raise PlanReviewError(f"{PLAN_REVIEW_FILE} is unreadable")
        raise PlanReviewError("no plan-review.json in this workspace")
    if review.state == CONSUMED:
        raise PlanReviewError("plan already approved and consumed by a running loop")
    parsed = _require_valid_plan(ws)
    review.state = APPROVED
    review.reviewed_at = _utcnow()
    review.note = note
    review.todolist_digest = todolist_digest(ws)
    review.stats = summarize(parsed)
    review.save(ws)
    return review


def _require_valid_plan(ws: WorkspacePaths) -> Todolist:
    """Parse ``todolist.md`` and hold it to the planner's invariant.

    An approved plan is about to be executed *as if the planner had written it*,
    so it must satisfy the same rules: unique ids, known types and statuses, at
    least one item, and nothing already ``done``. Only checking "parses and has
    items" leaves real holes — an edited all-``done`` plan consumes the gate and
    reports SUCCESS having executed nothing, and duplicate ids or unknown
    statuses wedge scheduling later.
    """
    if not ws.todolist.exists():
        raise PlanReviewError("no todolist.md to approve")
    from .todolist import Todolist as _Todolist, parse as parse_todolist
    from .validator import ValidationError, validate_transition

    try:
        parsed = parse_todolist(ws)
    except Exception as e:  # noqa: BLE001 — any parse failure is a bad plan
        raise PlanReviewError(f"todolist.md does not parse: {e}") from e
    if not parsed.items:
        raise PlanReviewError("todolist.md contains no items")
    try:
        validate_transition(_Todolist(), parsed, "planner", None)
    except ValidationError as e:
        raise PlanReviewError(f"todolist.md is not a valid initial plan: {e}") from e
    return parsed


def reject(ws: WorkspacePaths, *, note: str | None = None) -> PlanReview:
    """Mark the gate rejected and persist the note for the next planner run.

    The note lands in ``plan-rejection.md`` (a separate file) because
    ``design.md`` is read-only for every actor including the human-facing
    tooling — DESIGN §3. A future ``--fresh`` re-plan feeds this file to the
    planner so the same flawed plan isn't regenerated verbatim.
    """
    review = PlanReview.load(ws)
    if review is None:
        if gate_file_present(ws):
            raise PlanReviewError(f"{PLAN_REVIEW_FILE} is unreadable")
        raise PlanReviewError("no plan-review.json in this workspace")
    if review.state == CONSUMED:
        # A stale reject arriving after the loop already ran must not flip a
        # finished workspace back to `rejected` — status derivation prioritizes
        # the gate, so a completed run would be presented as plan_rejected.
        raise PlanReviewError("plan already approved and consumed by a running loop")
    review.state = REJECTED
    review.reviewed_at = _utcnow()
    review.note = note
    review.save(ws)
    if note:
        rejection_note_path(ws).write_text(
            f"# Plan rejected {review.reviewed_at}\n\n{note.strip()}\n",
            encoding="utf-8",
        )
    return review


def consume(ws: WorkspacePaths) -> None:
    """Mark an approved gate as consumed once the loop enters phase 1.

    Without this, a loop that exhausts and is later resumed would re-check an
    ``approved`` gate against a todolist the loop itself has since mutated
    (statuses advance, attempt_logs grow), see a digest mismatch, and bounce
    back to ``awaiting`` — demanding re-approval of a plan already underway.
    """
    review = PlanReview.load(ws)
    if review is None or review.state != APPROVED:
        return
    review.state = CONSUMED
    review.save(ws)


def mark_notified(ws: WorkspacePaths) -> None:
    """Record that a review request was pushed for the current episode."""
    review = PlanReview.load(ws)
    if review is None or review.state != AWAITING:
        return
    review.notified_at = _utcnow()
    review.save(ws)


class PlanReviewError(Exception):
    pass


@dataclass
class GateCheck:
    """Outcome of consulting the gate before phase 1."""

    proceed: bool
    reason: str = ""
    reverted: bool = False


def check_gate(ws: WorkspacePaths, *, enabled: bool) -> GateCheck:
    """Decide whether the loop may enter phase 1.

    Called on every ``run()``, including resumes. Returns ``proceed=False``
    when the loop must exit and wait for a human.

    ``enabled=False`` (policy ``never``) short-circuits to proceed so existing
    workspaces and CLI users keep today's behavior exactly.
    """
    if not enabled:
        return GateCheck(proceed=True)

    review = PlanReview.load(ws)
    if review is None:
        if gate_file_present(ws):
            # The file is there but unreadable (truncated / hand-edited /
            # invalid state). Fail closed: treating it as "no gate" would let
            # an unapproved plan execute, which is the one outcome this module
            # exists to prevent. A human can delete the file or re-plan with
            # --fresh to get out.
            return GateCheck(
                proceed=False,
                reason=(
                    f"{PLAN_REVIEW_FILE} is unreadable — refusing to execute an "
                    "unapproved plan; delete it or re-plan with --fresh"
                ),
            )
        # No gate file at all: either the policy was just turned on for a
        # workspace mid-flight, or the planner ran before this feature existed.
        # Opening a gate here would demand approval for a plan already in
        # progress, so only gate when nothing has happened yet.
        return GateCheck(proceed=True, reason="no gate file")

    if review.state == CONSUMED:
        return GateCheck(proceed=True, reason="gate already consumed")

    if review.state == REJECTED:
        note = f": {review.note}" if review.note else ""
        return GateCheck(
            proceed=False,
            reason=f"plan rejected by reviewer{note} — re-plan with --fresh",
        )

    if review.state == AWAITING:
        return GateCheck(proceed=False, reason="awaiting human plan review")

    # APPROVED — verify the plan hasn't drifted since the human signed off.
    current = todolist_digest(ws)
    if current != review.todolist_digest:
        approved_digest = review.todolist_digest
        review.state = AWAITING
        review.reviewed_at = None
        review.todolist_digest = current
        # New awaiting episode — the reviewer must be told the plan drifted.
        review.notified_at = None
        review.save(ws)
        return GateCheck(
            proceed=False,
            reason=(
                "todolist changed after approval — re-approval required "
                f"(approved {approved_digest[:12]}… ≠ current {current[:12]}…)"
            ),
            reverted=True,
        )

    consume(ws)
    return GateCheck(proceed=True, reason="plan approved")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
