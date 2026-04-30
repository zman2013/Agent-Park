"""LoopState — persisted state for the scheduler.

Holds only the fields the scheduler needs for termination and exhaustion
checks. The todolist.md file is the authoritative source for item state; this
file only tracks cycle count, accumulated cost, the last PM decision (for
three-in-a-row detection), and bookkeeping like rollbacks.

Persistence location is decided by :class:`agentloop.workspace.WorkspacePaths`:
``<cwd>/.agentloop/workspaces/<slug>/state.json``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .workspace import WorkspacePaths


@dataclass
class Decision:
    next: str              # "dev" | "qa" | "done"
    item_id: str | None
    reason: str = ""

    def key(self) -> tuple[str, str | None]:
        return (self.next, self.item_id)


@dataclass
class Limits:
    max_cycles: int = 60
    max_item_attempts: int = 5
    max_cost_cny: float = 1000.0
    max_planner_attempts: int = 3
    max_fingerprint_stuck: int = 4


@dataclass
class LoopState:
    cycle: int = 0
    total_cost_cny: float = 0.0
    last_decision: Decision | None = None
    same_decision_count: int = 0
    started_at: str = ""
    exhausted_reason: str | None = None
    rollbacks: list[dict[str, Any]] = field(default_factory=list)
    fingerprint_history: list[str] = field(default_factory=list)
    abandoned_events: list[dict[str, Any]] = field(default_factory=list)
    scheduler_events: list[dict[str, Any]] = field(default_factory=list)
    planner_attempts: int = 0

    # ----- persistence ---------------------------------------------------

    @classmethod
    def load_or_init(cls, ws: WorkspacePaths) -> "LoopState":
        path = ws.state_file
        if not path.exists():
            return cls(started_at=_utcnow())
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(started_at=_utcnow())
        last = data.get("last_decision")
        decision = (
            Decision(last["next"], last.get("item_id"), last.get("reason", ""))
            if last
            else None
        )
        return cls(
            cycle=int(data.get("cycle", 0)),
            total_cost_cny=float(data.get("total_cost_cny", 0.0)),
            last_decision=decision,
            same_decision_count=int(data.get("same_decision_count", 0)),
            started_at=data.get("started_at") or _utcnow(),
            exhausted_reason=data.get("exhausted_reason"),
            rollbacks=list(data.get("rollbacks", [])),
            fingerprint_history=list(data.get("fingerprint_history", [])),
            abandoned_events=list(data.get("abandoned_events", [])),
            scheduler_events=list(data.get("scheduler_events", [])),
            planner_attempts=int(data.get("planner_attempts", 0)),
        )

    def save(self, ws: WorkspacePaths) -> None:
        path = ws.state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cycle": self.cycle,
            "total_cost_cny": self.total_cost_cny,
            "last_decision": asdict(self.last_decision) if self.last_decision else None,
            "same_decision_count": self.same_decision_count,
            "started_at": self.started_at,
            "exhausted_reason": self.exhausted_reason,
            "rollbacks": self.rollbacks,
            "fingerprint_history": self.fingerprint_history,
            "abandoned_events": self.abandoned_events,
            "scheduler_events": self.scheduler_events,
            "planner_attempts": self.planner_attempts,
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # ----- scheduler hooks -----------------------------------------------

    def should_exit(self, limits: Limits) -> str | None:
        if self.exhausted_reason:
            return self.exhausted_reason
        if self.cycle >= limits.max_cycles:
            return f"max_cycles reached ({limits.max_cycles})"
        if self.total_cost_cny >= limits.max_cost_cny:
            return f"max_cost reached ({self.total_cost_cny:.2f} >= {limits.max_cost_cny})"
        # v2: removed same_decision_count early exit — the loop now relies on
        # fuse / reconcile / fingerprint_stuck for convergence. The counter
        # itself is still tracked on LoopState for diagnostics.
        return None

    def record_decision(self, decision: Decision) -> None:
        if self.last_decision is not None and self.last_decision.key() == decision.key():
            self.same_decision_count += 1
        else:
            self.same_decision_count = 1
        self.last_decision = decision

    def record_cost(self, cost_cny: float) -> None:
        if cost_cny > 0:
            self.total_cost_cny += cost_cny

    def record_rollback(self, actor: str, item_id: str | None, error: str) -> None:
        self.rollbacks.append(
            {
                "cycle": self.cycle,
                "actor": actor,
                "item_id": item_id,
                "error": error,
                "at": _utcnow(),
            }
        )

    def mark_exhausted(self, reason: str) -> None:
        self.exhausted_reason = reason


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
