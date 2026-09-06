"""Manage independent agentloop subprocesses.

agentloop is a separate CLI under ``agentloop/`` that drives a
planner/PM/dev/qa pipeline against a ``design.md`` file inside a target cwd.
Each run lives in its own workspace at
``<cwd>/.agentloop/workspaces/<slug>/`` (state.json, todolist.md, runs/,
stdout.log, design.md symlink).

This module spawns those processes detached from any agent-park task, tracks
them in a registry, recovers orphans on restart, and serves snapshots to the
UI.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentloop.workspace import (
    AGENTLOOP_DIR,
    DESIGN_FILE,
    RUNS_SUBDIR,
    STATE_FILE,
    STDOUT_LOG,
    TODOLIST_FILE,
    WORKSPACES_SUBDIR,
    WorkspacePaths,
    generate_slug,
)
from agentloop.config import seed_workspace_config

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REGISTRY_FILE = DATA_DIR / "agentloops.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loop_id(cwd: str, slug: str) -> str:
    """Stable 8-char id derived from cwd + workspace slug."""
    key = f"{cwd}\n{slug}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


class _LegacyWorkspacePaths:
    """Pre-workspace layout adapter for registry entries without a ``workspace``.

    Older ``data/agentloops.json`` rows predate multi-workspace support: state
    and runs lived directly under ``<cwd>/.agentloop/`` and todolist.md in the
    project root. We expose the same duck-typed attributes as
    :class:`WorkspacePaths` so downstream helpers keep working without forcing
    a registry migration.
    """

    def __init__(self, cwd: Path) -> None:
        self._cwd = Path(cwd).resolve()
        self.slug = ""

    @property
    def workspace_dir(self) -> Path:
        return self._cwd / AGENTLOOP_DIR

    @property
    def state_file(self) -> Path:
        return self._cwd / AGENTLOOP_DIR / STATE_FILE

    @property
    def todolist(self) -> Path:
        return self._cwd / TODOLIST_FILE

    @property
    def runs_dir(self) -> Path:
        return self._cwd / AGENTLOOP_DIR / RUNS_SUBDIR

    @property
    def design(self) -> Path:
        return self._cwd / DESIGN_FILE

    @property
    def stdout_log(self) -> Path:
        return self._cwd / AGENTLOOP_DIR / STDOUT_LOG


def _entry_workspace(entry: dict[str, Any]) -> WorkspacePaths | _LegacyWorkspacePaths:
    """Build a workspace-paths object for an existing registry entry.

    Preference order:
    1. Explicit ``workspace_dir`` field (new entries) → direct construct.
    2. ``cwd + workspace`` slug pair (current entries before this refactor) →
       compose via :meth:`WorkspacePaths.for_workspace`.
    3. ``cwd`` only → :class:`_LegacyWorkspacePaths` (pre-workspace layout).
    """
    ws_dir = entry.get("workspace_dir")
    if ws_dir:
        return WorkspacePaths.from_workspace_dir(Path(ws_dir))

    slug = entry.get("workspace")
    cwd = entry.get("cwd")
    if not cwd:
        raise ValueError(
            f"registry entry {entry.get('loop_id')!r} missing required 'cwd'"
        )
    if not slug:
        return _LegacyWorkspacePaths(Path(cwd))
    return WorkspacePaths.for_workspace(Path(cwd), slug)


def _load_registry() -> list[dict[str, Any]]:
    if not REGISTRY_FILE.exists():
        return []
    try:
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to load agentloops registry; returning empty")
        return []


def _save_registry(entries: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(REGISTRY_FILE)


def _upsert(entry: dict[str, Any]) -> None:
    entries = _load_registry()
    for i, e in enumerate(entries):
        if e.get("loop_id") == entry["loop_id"]:
            entries[i] = entry
            break
    else:
        entries.append(entry)
    _save_registry(entries)


def _update_fields(loop_id: str, **fields: Any) -> dict[str, Any] | None:
    entries = _load_registry()
    for e in entries:
        if e.get("loop_id") == loop_id:
            e.update(fields)
            _save_registry(entries)
            return e
    return None


def _find(loop_id: str) -> dict[str, Any] | None:
    for e in _load_registry():
        if e.get("loop_id") == loop_id:
            return e
    return None


def _pid_alive(pid: int) -> bool:
    """Return True only if pid exists and is not a zombie.

    ``os.kill(pid, 0)`` succeeds for zombies, which would keep a finished loop
    stuck in ``running`` status (we launch detached and never waitpid, so a
    crashed child lingers as a zombie until reaped by init when we exit the
    session group). We additionally try a non-blocking reap — harmless if pid
    is not our child — and check ``/proc/<pid>/status`` State to filter Z.
    """
    if pid <= 0:
        return False
    try:
        reaped_pid, _ = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("State:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == "Z":
                        return False
                    break
    except (FileNotFoundError, OSError):
        return False
    return True


def _proc_start_time(pid: int) -> int | None:
    """Return pid's start time in clock ticks since boot, or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
    except (FileNotFoundError, OSError):
        return None
    rparen = data.rfind(b")")
    if rparen < 0:
        return None
    fields = data[rparen + 2:].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _pid_matches(pid: int, expected_start_time: int | None) -> bool:
    """True if pid is alive AND (if expected given) its start time still matches."""
    if not _pid_alive(pid):
        return False
    if expected_start_time is None:
        return True
    current = _proc_start_time(pid)
    return current is not None and current == expected_start_time


def _read_state(ws: WorkspacePaths) -> dict[str, Any] | None:
    state_path = ws.state_file
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_plan_review(ws: WorkspacePaths) -> dict[str, Any] | None:
    """Read the workspace's plan-review gate file.

    Returns ``None`` when no gate exists, and a synthetic ``unreadable`` entry
    when the file is there but corrupt. Collapsing the two would report the
    loop as ``stopped`` — the panel would hide the review banner and its
    recovery controls, while Start just repeats the same fail-closed exit.
    """
    try:
        from agentloop.plan_review import PLAN_REVIEW_FILE, VALID_STATES, stats_ok
    except ImportError:
        PLAN_REVIEW_FILE = "plan-review.json"
        VALID_STATES = {"awaiting", "approved", "rejected", "consumed"}
        stats_ok = lambda s: isinstance(s, dict)  # noqa: E731
    path = ws.workspace_dir / PLAN_REVIEW_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"state": "unreadable"}
    if not isinstance(data, dict):
        return {"state": "unreadable"}
    # Mirror PlanReview.load's acceptance criteria exactly: anything it rejects
    # makes the scheduler fail closed, so the manager must show the paused
    # recovery banner rather than falling through to `stopped`.
    if str(data.get("state") or "") not in VALID_STATES:
        return {"state": "unreadable"}
    if not stats_ok(data.get("stats") or {}):
        return {"state": "unreadable"}
    return data


def _derive_status_from_state(state: dict[str, Any] | None, ws: WorkspacePaths | None = None) -> str:
    """Infer a finished loop's status from its state.json (+ gate file).

    The plan-review gate is checked *first*: a loop that exited awaiting human
    approval has no ``exhausted_reason`` and no ``done`` decision, so without
    this branch it would be reported as ``stopped`` — indistinguishable from a
    user-initiated kill, and the UI would offer no approve action.
    """
    if ws is not None:
        review = _read_plan_review(ws)
        if review:
            gate_state = str(review.get("state") or "")
            if gate_state in ("awaiting", "unreadable"):
                # `unreadable` is a paused state too: the loop refuses to run
                # until a human fixes or deletes the gate file.
                return "awaiting_review"
            if gate_state == "rejected":
                return "plan_rejected"
    if not state:
        return "unknown"
    if state.get("exhausted_reason"):
        return "exhausted"
    last = state.get("last_decision") or {}
    if last.get("next") == "done":
        if state.get("abandoned_events"):
            return "partial"
        return "done"
    return "stopped"


# ── public API ────────────────────────────────────────────────────────────────


def start(
    cwd: str | Path,
    design_path: str | Path | None = None,
    source_task_id: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Spawn an agentloop process detached from the current session.

    If ``workspace`` is omitted, a timestamp-derived slug is generated. On
    slug collision (same UTC second, rare) a 4-char uuid suffix is appended;
    after 5 retries the call fails with RuntimeError.
    """
    cwd_path = Path(str(cwd)).resolve()
    if not cwd_path.is_dir():
        raise ValueError(f"cwd does not exist or is not a directory: {cwd_path}")

    design = Path(str(design_path)) if design_path else cwd_path / DESIGN_FILE
    if not design.is_absolute():
        design = (cwd_path / design).resolve()
    if not design.is_file():
        raise ValueError(f"design file not found: {design}")
    design_target = design.resolve()

    # Allocate a workspace slug. Two flows:
    #   • explicit slug → honor idempotency first (running loop in same
    #     workspace returns as-is); otherwise reuse or create the directory
    #     so retries/restarts against a known slug work.
    #   • auto slug → generate, then mkdir(exist_ok=False) with uuid retry
    #     to guarantee a fresh directory.
    slug = workspace
    if slug:
        ws = WorkspacePaths.for_workspace(cwd_path, slug)
        loop_id = _loop_id(str(cwd_path), slug)
        existing = _find(loop_id)
        if existing and existing.get("status") == "running":
            pid = existing.get("pid")
            expected_st = existing.get("pid_start_time")
            if pid and _pid_matches(int(pid), expected_st):
                logger.info(
                    "agentloop already running for %s/%s (pid=%s)",
                    cwd_path, slug, pid,
                )
                return existing
        # Reuse or create the workspace dir. A prior stopped/exhausted run in
        # the same slug stays on disk so restart flows can pick it up.
        ws.workspace_dir.mkdir(parents=True, exist_ok=True)
    else:
        base = generate_slug(design_target)
        for attempt in range(5):
            candidate = base if attempt == 0 else f"{base}-{uuid.uuid4().hex[:4]}"
            ws = WorkspacePaths.for_workspace(cwd_path, candidate)
            try:
                ws.workspace_dir.mkdir(parents=True, exist_ok=False)
                slug = candidate
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError(
                f"could not allocate a unique workspace slug under {cwd_path}"
            )
        loop_id = _loop_id(str(cwd_path), slug)

    # Put the design symlink inside the workspace dir. The CLI reads it from
    # ws.design, the planner agent template resolves {{cwd}} to workspace_dir.
    # Subprocess cwd stays at project root (so it can still read project files,
    # config, run git commands).
    design_in_ws = ws.design
    if design_in_ws.exists() or design_in_ws.is_symlink():
        # Reused workspace: only replace if the target changed to avoid
        # disturbing an already-running loop's open fd.
        try:
            current = design_in_ws.resolve()
        except OSError:
            current = None
        if current != design_target:
            try:
                design_in_ws.unlink()
            except OSError:
                pass
    if not design_in_ws.exists() and not design_in_ws.is_symlink():
        try:
            design_in_ws.symlink_to(design_target)
        except OSError:
            # Cross-device or unsupported symlink target → fall back to copy.
            import shutil as _sh
            _sh.copy2(design_target, design_in_ws)
            logger.warning(
                "symlink %s → %s failed; copied instead (design edits won't propagate)",
                design_in_ws, design_target,
            )

    # Seed config.toml from the user global (or leave unseeded, in which case
    # the loader falls back to built-in defaults). Idempotent — won't overwrite
    # a hand-edited per-workspace config.
    try:
        seed_workspace_config(ws.workspace_dir)
    except OSError:
        logger.warning("failed to seed %s", ws.config_file)

    # Inject the project-level Feishu bot config into the workspace so the
    # agentloop summary stage can notify on completion without requiring a
    # second source of truth. Reuses ``wiki_ingest.feishu_notify`` — the same
    # bot/chat serves both pipelines (see design in this PR).
    try:
        _inject_feishu_into_workspace_config(ws.config_file)
    except Exception:  # noqa: BLE001
        logger.exception("failed to inject feishu config into %s", ws.config_file)

    stdout_log = ws.stdout_log
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(stdout_log, "ab", buffering=0)

    # Launch with start_new_session=True so the child lives beyond us.
    repo_root = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "PYTHONPATH": str(repo_root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }

    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agentloop",
                "run",
                # Pass the original design path and the absolute workspace dir.
                # --workspace-dir is the authoritative pointer — the CLI no
                # longer derives project_root from design.parent, which was the
                # source of the nested-bootstrap bug.
                str(design_target),
                "--workspace-dir",
                str(ws.workspace_dir),
            ],
            cwd=str(cwd_path),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    finally:
        log_fp.close()

    entry = {
        "loop_id": loop_id,
        "cwd": str(cwd_path),
        "workspace": slug,
        "workspace_dir": str(ws.workspace_dir),
        "design_path": str(design_target),
        "pid": proc.pid,
        "pid_start_time": _proc_start_time(proc.pid),
        "started_at": _utcnow(),
        "source_task_id": source_task_id,
        "status": "running",
        "dismissed": False,
        "last_seen_cycle": 0,
    }
    _upsert(entry)
    logger.info(
        "Started agentloop pid=%s loop_id=%s cwd=%s workspace=%s",
        proc.pid, loop_id, cwd_path, slug,
    )
    return entry


def stop(loop_id: str, timeout_sec: float = 10.0) -> dict[str, Any] | None:
    entry = _find(loop_id)
    if not entry:
        return None
    pid = int(entry.get("pid") or 0)
    expected_st = entry.get("pid_start_time")
    if pid and _pid_matches(pid, expected_st):
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

        deadline = time.time() + timeout_sec
        while time.time() < deadline and _pid_matches(pid, expected_st):
            time.sleep(0.2)

        if _pid_matches(pid, expected_st):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    updated = _update_fields(
        loop_id,
        status="stopped",
        stopped_at=_utcnow(),
    )
    return updated


def dismiss(loop_id: str) -> dict[str, Any] | None:
    """Hide the loop from the sidebar 'recent' list. Does not kill the process."""
    return _update_fields(loop_id, dismissed=True)


def review_plan(
    loop_id: str,
    *,
    approve: bool,
    note: str | None = None,
    todolist: str | None = None,
) -> dict[str, Any] | None:
    """Approve or reject a loop's pending plan.

    ``todolist`` optionally replaces ``todolist.md`` before the gate is bound,
    so a reviewer can correct the plan and approve in one atomic call. The
    approval digest covers the written content, which is what the loop verifies
    on startup. It is honored on approval only — a rejection leaves the
    persisted plan untouched.

    On approval the loop process is relaunched against the same workspace: the
    todolist and state.json are still on disk, so the planner is skipped and
    execution resumes at phase 1.

    Returns the refreshed entry, or ``None`` when ``loop_id`` is unknown.
    Raises ``ValueError`` when there is no gate to act on, or when the loop is
    still running (approving mid-flight would spawn a second process against
    one workspace).
    """
    entry = _find(loop_id)
    if not entry:
        return None

    pid = int(entry.get("pid") or 0)
    if _pid_matches(pid, entry.get("pid_start_time")):
        raise ValueError(
            "loop process is still running — stop it before reviewing the plan"
        )

    ws = _entry_workspace(entry)
    try:
        from agentloop import plan_review as pr
    except ImportError as e:  # pragma: no cover - packaging error
        raise ValueError(f"agentloop package unavailable: {e}") from e

    if not isinstance(ws, WorkspacePaths):
        raise ValueError("legacy workspace layout does not support plan review")

    # Validate the gate *before* touching todolist.md. A stale or already
    # consumed review request must fail without having replaced the persisted
    # plan, and rejection must never overwrite it at all — the UI sends the
    # editor contents whenever edit mode is open, but we promise edits are
    # saved on approval only. The consumed check covers rejection too: flipping
    # a finished loop's gate to `rejected` would make status derivation present
    # a completed run as plan_rejected.
    gate = pr.PlanReview.load(ws)
    if gate is None:
        if pr.gate_file_present(ws):
            raise ValueError(f"{pr.PLAN_REVIEW_FILE} is unreadable in this workspace")
        raise ValueError("no plan-review.json in this workspace")
    if gate.state == pr.CONSUMED:
        raise ValueError("plan already approved and consumed by a running loop")

    if approve and todolist is not None:
        # Validate before overwriting: an invalid todolist would wedge the loop
        # on its next start, and the reviewer would have lost the plan. Same
        # invariant `pr.approve` enforces (planner shape: unique ids, known
        # types/statuses, nothing already done) — applied here so a bad edit
        # never reaches disk.
        try:
            from agentloop.todolist import Todolist, parse_text
            from agentloop.validator import validate_transition
        except ImportError:
            parse_text = None
        if parse_text is not None:
            try:
                parsed = parse_text(todolist)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"edited todolist does not parse: {e}") from e
            if not parsed.items:
                raise ValueError("edited todolist contains no items")
            try:
                validate_transition(Todolist(), parsed, "planner", None)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"edited todolist is not a valid plan: {e}") from e
        ws.todolist.write_text(todolist, encoding="utf-8")

    try:
        if approve:
            pr.approve(ws, note=note)
        else:
            pr.reject(ws, note=note)
    except pr.PlanReviewError as e:
        raise ValueError(str(e)) from e

    if not approve:
        # Clear ``notified_at``: the source task was very likely already told
        # about the `awaiting_review` episode, and keeping the stamp makes
        # ``notify_source_task`` skip the newly-notifiable `plan_rejected`
        # status — the original conversation would sit forever showing the plan
        # as awaiting approval, never learning a re-plan is needed.
        updated = _update_fields(
            loop_id,
            status="plan_rejected",
            reviewed_at=_utcnow(),
            notified_at=None,
        )
        return _summary(updated or entry)

    # Relaunch against the same workspace slug. ``start`` is idempotent on a
    # running loop and reuses an existing workspace dir, so this resumes rather
    # than creating a sibling.
    return start(
        cwd=entry["cwd"],
        design_path=entry.get("design_path"),
        source_task_id=entry.get("source_task_id"),
        workspace=entry.get("workspace"),
    )


def _refresh_status(entry: dict[str, Any]) -> dict[str, Any]:
    """Reconcile an entry's ``status`` field with actual process/state on disk."""
    pid = int(entry.get("pid") or 0)
    expected_st = entry.get("pid_start_time")
    ws = _entry_workspace(entry)
    state = _read_state(ws)

    if entry.get("status") == "running":
        if not _pid_matches(pid, expected_st):
            derived = _derive_status_from_state(state, ws)
            _update_fields(entry["loop_id"], status=derived, stopped_at=_utcnow())
            entry["status"] = derived

    if state:
        cycle = int(state.get("cycle", 0))
        if cycle != entry.get("last_seen_cycle"):
            _update_fields(entry["loop_id"], last_seen_cycle=cycle)
            entry["last_seen_cycle"] = cycle

    return entry


def list_all(*, include_dismissed: bool = True) -> list[dict[str, Any]]:
    """Return all registry entries with status reconciled + state summary."""
    entries = _load_registry()
    result: list[dict[str, Any]] = []
    for entry in entries:
        entry = _refresh_status(entry)
        if not include_dismissed and entry.get("dismissed"):
            continue
        summary = _summary(entry)
        result.append(summary)
    result.sort(key=lambda e: e.get("started_at", ""), reverse=True)
    return result


def list_recent(limit: int = 5, days: int = 7) -> list[dict[str, Any]]:
    """Sidebar 'recent updates' list."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out: list[dict[str, Any]] = []
    for e in list_all(include_dismissed=False):
        started = e.get("started_at", "")
        try:
            ts = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            ts = 0.0
        if ts < cutoff:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def get_snapshot(loop_id: str) -> dict[str, Any] | None:
    entry = _find(loop_id)
    if not entry:
        return None
    entry = _refresh_status(entry)
    summary = _summary(entry)
    ws = _entry_workspace(entry)
    summary["state"] = _read_state(ws)
    summary["todolist"] = _read_todolist(ws)
    summary["runs"] = _list_runs(ws)
    return summary


def get_run_log(loop_id: str, cycle: int) -> list[dict[str, Any]] | None:
    entry = _find(loop_id)
    if not entry:
        return None
    ws = _entry_workspace(entry)
    runs_dir = ws.runs_dir
    if not runs_dir.is_dir():
        return None
    prefix = f"{cycle:03d}-"
    match = next((p for p in sorted(runs_dir.iterdir()) if p.name.startswith(prefix)), None)
    if match is None:
        return []
    lines: list[dict[str, Any]] = []
    try:
        with match.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    lines.append({"type": "_parse_error", "raw": raw})
    except OSError:
        return None
    return lines


def restore_orphan_loops() -> list[str]:
    """On server startup, reconcile registry against live processes."""
    results: list[str] = []
    for entry in _load_registry():
        loop_id = entry.get("loop_id")
        if not loop_id:
            continue
        if entry.get("status") != "running":
            continue
        pid = int(entry.get("pid") or 0)
        expected_st = entry.get("pid_start_time")
        if _pid_matches(pid, expected_st):
            logger.info("Re-claimed orphan agentloop pid=%s loop_id=%s", pid, loop_id)
            continue
        ws = _entry_workspace(entry)
        state = _read_state(ws)
        derived = _derive_status_from_state(state, ws)
        _update_fields(loop_id, status=derived, stopped_at=_utcnow())
        results.append(loop_id)
    return results


# ── helpers ──────────────────────────────────────────────────────────────────


def _inject_feishu_into_workspace_config(config_file: Path) -> None:
    """Write the project-level Feishu config into a workspace ``config.toml``.

    Reuses ``wiki_ingest.feishu_notify`` from the project's ``config.json`` so
    the same bot/chat serves both pipelines — the user only configures it once.

    Idempotent and non-destructive:
      * If the workspace config already has a ``[summary.feishu]`` section
        with a non-empty ``cli_path``, we do nothing — respect hand edits.
      * If the project config.json has no Feishu values configured, we do
        nothing — otherwise agentloop would try to notify against an empty
        chat_id and log warnings forever.
      * Otherwise append a fresh ``[summary]`` + ``[summary.feishu]`` block.
    """
    from .config import wiki_ingest_config

    wi = wiki_ingest_config()
    feishu = wi.get("feishu_notify") or {}
    cli_path = (feishu.get("cli_path") or "").strip()
    chat_id = (feishu.get("chat_id") or "").strip()
    env_file = (feishu.get("env_file") or "").strip()

    # Only inject when the project has actually configured a bot. An
    # "enabled: false" project config is a valid opt-out — we honor it by
    # refusing to inject values that the user explicitly disabled.
    project_enabled = bool(feishu.get("enabled"))
    if not project_enabled or not cli_path or not chat_id:
        return

    existing = ""
    if config_file.exists():
        try:
            existing = config_file.read_text(encoding="utf-8")
        except OSError:
            return
    # Coarse but sufficient guard — if the user already filled in real feishu
    # values we don't want to double-inject or contradict their edits. But an
    # empty placeholder [summary.feishu] section (created by a template or
    # prior partial write) should still trigger injection so that valid
    # project-level config reaches the workspace.
    if _has_populated_feishu_section(existing):
        return

    has_empty_feishu_section = "[summary.feishu]" in existing

    if has_empty_feishu_section:
        # Replace the existing (empty) [summary.feishu] section in place,
        # rather than appending another one — TOML forbids redeclaration.
        new_text = _replace_feishu_section(
            existing, cli_path=cli_path, chat_id=chat_id, env_file=env_file
        )
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(new_text, encoding="utf-8")
        return

    # Detect pre-existing [summary] table. TOML forbids redeclaring the same
    # table, so appending a fresh [summary] block on top of an existing one
    # produces a parse error; AgentConfig._merge_from() then silently drops
    # the whole file on TOMLDecodeError, losing user limits/flags.
    # Match [summary] as its own table (not [summary.feishu] or similar).
    has_summary_table = bool(
        re.search(r"(?m)^\s*\[summary\]\s*$", existing)
    )

    if has_summary_table:
        block = [
            "",
            "# Injected by agentloop_manager from project config.json.",
            "# Edit here to override for this workspace only.",
            "[summary.feishu]",
            f'cli_path = "{cli_path}"',
            f'chat_id = "{chat_id}"',
            f'env_file = "{env_file}"',
            "",
        ]
    else:
        block = [
            "",
            "# Injected by agentloop_manager from project config.json.",
            "# Edit here to override for this workspace only.",
            "[summary]",
            "enabled = true",
            "feishu_enabled = true",
            "",
            "[summary.feishu]",
            f'cli_path = "{cli_path}"',
            f'chat_id = "{chat_id}"',
            f'env_file = "{env_file}"',
            "",
        ]
    new_text = existing
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    new_text += "\n".join(block)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(new_text, encoding="utf-8")


def _has_populated_feishu_section(text: str) -> bool:
    """True iff ``text`` contains a [summary.feishu] section that the user
    appears to have managed themselves — we should NOT overwrite.

    Policy: only treat the section as a blank template placeholder
    (overwrite-able) when BOTH ``cli_path`` and ``chat_id`` are present and
    both parse to empty double-quoted strings (``"" ``). Anything else counts
    as user-managed:
      * a non-empty value in either key
      * single-quoted values (``'...'`` — valid TOML, user-authored)
      * only one of the two keys (deliberate partial config)
      * any other form (multiline strings, numbers, etc.)
    Matching the exact empty-double-quoted pair is narrow on purpose: it is
    the canonical empty-template shape, and anything that deviates from it
    is more likely intentional than accidental.
    """
    if "[summary.feishu]" not in text:
        return False
    m = re.search(r"(?m)^\s*\[summary\.feishu\]\s*$", text)
    if not m:
        return False
    start = m.end()
    next_header = re.search(r"(?m)^\s*\[[^\]]+\]\s*$", text[start:])
    section = text[start : start + next_header.start()] if next_header else text[start:]
    cli_key = re.search(r"(?m)^\s*cli_path\s*=\s*(.*)$", section)
    chat_key = re.search(r"(?m)^\s*chat_id\s*=\s*(.*)$", section)
    if not cli_key or not chat_key:
        # At least one key missing — treat as user-managed (deliberate partial
        # config) to avoid stomping on single-key edits.
        return True
    cli_val = cli_key.group(1).strip()
    chat_val = chat_key.group(1).strip()
    # Empty-template canonical shape: both values are exactly "".
    is_blank_template = cli_val == '""' and chat_val == '""'
    return not is_blank_template


def _replace_feishu_section(
    text: str, *, cli_path: str, chat_id: str, env_file: str
) -> str:
    """Replace an existing (empty) [summary.feishu] section body with populated
    values. Preserves any content before the section header and after the
    section ends (next [table] header or EOF).
    """
    m = re.search(r"(?m)^\s*\[summary\.feishu\]\s*$", text)
    if not m:
        return text  # guarded by caller, but be defensive
    head = text[: m.end()]
    tail_start = m.end()
    next_header = re.search(r"(?m)^\s*\[[^\]]+\]\s*$", text[tail_start:])
    tail = text[tail_start + next_header.start() :] if next_header else ""
    body = (
        "\n"
        f'cli_path = "{cli_path}"\n'
        f'chat_id = "{chat_id}"\n'
        f'env_file = "{env_file}"\n'
    )
    if tail:
        return head + body + ("\n" + tail if not tail.startswith("\n") else tail)
    return head + body


def _summary(entry: dict[str, Any]) -> dict[str, Any]:
    """Shallow-copy the entry and tack on a small state summary."""
    out = dict(entry)
    ws = _entry_workspace(entry)
    state = _read_state(ws)
    if state:
        out["cycle"] = int(state.get("cycle", 0))
        out["total_cost_cny"] = float(state.get("total_cost_cny", 0.0))
        out["exhausted_reason"] = state.get("exhausted_reason")
    else:
        out["cycle"] = 0
        out["total_cost_cny"] = 0.0
        out["exhausted_reason"] = None
    out["cwd_basename"] = Path(entry["cwd"]).name
    out["plan_review"] = _read_plan_review(ws)
    return out


def _read_todolist(ws: WorkspacePaths) -> dict[str, Any]:
    """Parse todolist.md using agentloop's own parser, returning a JSON dict."""
    try:
        from agentloop.todolist import parse as parse_todolist
    except ImportError:
        return {"metadata": {}, "items": [], "raw": ""}
    try:
        tl = parse_todolist(ws)
    except Exception:
        logger.exception("Failed to parse todolist for %s", ws.todolist)
        return {"metadata": {}, "items": [], "raw": ""}
    items = []
    for it in tl.items:
        items.append(
            {
                "id": it.id,
                "type": it.type,
                "status": it.status,
                "title": it.title,
                "dependencies": list(it.dependencies),
                "source": it.source,
                "dev_notes": it.dev_notes,
                "findings": it.findings,
                "attempt_log": [
                    {"cycle": a.cycle, "result": a.result, "notes": a.notes}
                    for a in it.attempt_log
                ],
            }
        )
    # ``raw`` backs the plan-review editor: the reviewer edits the actual file
    # content (which is what the approval digest binds to), not a re-serialized
    # projection of the parsed model.
    try:
        raw = ws.todolist.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    return {"metadata": dict(tl.metadata), "items": items, "raw": raw}


def _list_runs(ws: WorkspacePaths) -> list[dict[str, Any]]:
    runs_dir = ws.runs_dir
    if not runs_dir.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for p in sorted(runs_dir.iterdir()):
        name = p.name
        if not name.endswith(".jsonl"):
            continue
        stem = name[:-6]
        parts = stem.split("-", 2)
        try:
            cycle = int(parts[0])
        except (ValueError, IndexError):
            continue
        actor = parts[1] if len(parts) > 1 else ""
        item_id = parts[2] if len(parts) > 2 else ""
        try:
            size = p.stat().st_size
            mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except OSError:
            size = 0
            mtime = ""
        result.append(
            {
                "cycle": cycle,
                "actor": actor,
                "item_id": item_id,
                "filename": name,
                "size": size,
                "mtime": mtime,
            }
        )
    return result


# ── source-task notification ─────────────────────────────────────────────────
#
# When an agentloop is spawned via the /agentloop skill, the registry records
# the originating agent task as ``source_task_id``. The loop runs detached and
# the source task usually finishes long before the loop does. ``notify_source_task``
# closes that loop: once a loop transitions out of ``running``, we inject a
# ``type="system"`` message back into the source task with status + cycles +
# cost + summary.md, so the user sees the result inline in the original
# conversation without having to open the AgentLoop side panel.
#
# Triggered from two places: (a) every GET on /api/agentloops* refreshes status
# and fires a fire-and-forget call, and (b) ``_agentloop_notify_loop`` in
# routes_ws polls every 60 s as a fallback when no UI is open. ``notified_at``
# in the registry guarantees idempotency across both paths.

_SUMMARY_MAX_BYTES = 20 * 1024  # truncate summary.md beyond this in the message

_STATUS_DISPLAY: dict[str, tuple[str, str]] = {
    # status → (emoji, 中文标签)
    "done": ("✅", "完成"),
    "partial": ("⚠️", "部分完成"),
    "exhausted": ("⏱️", "资源耗尽"),
    "error": ("❌", "失败"),
    "unknown": ("❓", "未知"),
    # The gate states are notifiable on purpose: they are the only statuses
    # that *require* a human action before anything else can happen, so the
    # source task must be told rather than left waiting on a silent loop.
    "awaiting_review": ("⏸️", "待确认计划"),
    "plan_rejected": ("🚫", "计划被驳回"),
}

# Statuses where the loop is paused pending human input rather than finished.
# ``_render_completion_message`` swaps its header for these.
_PAUSED_STATUSES = frozenset({"awaiting_review", "plan_rejected"})

# Statuses that warrant notification. ``stopped`` is excluded on purpose:
# the user explicitly asked the loop to stop, so they don't need a poke.
# ``running`` is excluded because the loop hasn't actually finished yet.
_NOTIFIABLE_STATUSES = frozenset(_STATUS_DISPLAY.keys())

# Per-loop notification lock to prevent duplicate system-message injection when
# multiple triggers fire concurrently (e.g. an AgentLoop GET refresh racing the
# 60s background sweep). The lock plus pre-await ``notified_at`` stamp gives us
# a true compare-and-set under the asyncio event loop.
_NOTIFY_LOCKS: dict[str, asyncio.Lock] = {}


def _get_notify_lock(loop_id: str) -> asyncio.Lock:
    lock = _NOTIFY_LOCKS.get(loop_id)
    if lock is None:
        lock = asyncio.Lock()
        _NOTIFY_LOCKS[loop_id] = lock
    return lock


def _read_summary_md(ws: WorkspacePaths | _LegacyWorkspacePaths) -> str | None:
    """Read ``summary.md`` from the workspace, truncating if oversize."""
    summary_path = ws.workspace_dir / "summary.md"
    if not summary_path.is_file():
        return None
    try:
        data = summary_path.read_bytes()
    except OSError:
        return None
    if len(data) <= _SUMMARY_MAX_BYTES:
        return data.decode("utf-8", errors="replace")
    head = data[:_SUMMARY_MAX_BYTES].decode("utf-8", errors="replace")
    return head + "\n\n… (truncated, 完整版见 workspace 内 summary.md)"


def _render_completion_message(
    entry: dict[str, Any],
    ws: WorkspacePaths | _LegacyWorkspacePaths,
) -> str:
    """Build the markdown body of the system message injected into the source task."""
    status = (entry.get("status") or "unknown").lower()
    emoji, label = _STATUS_DISPLAY.get(status, _STATUS_DISPLAY["unknown"])

    state = _read_state(ws) or {}
    cycle = int(state.get("cycle", 0))
    total_cost = float(state.get("total_cost_cny", 0.0))
    exhausted_reason = state.get("exhausted_reason") or "(完成)"
    workspace_dir = entry.get("workspace_dir") or str(ws.workspace_dir)
    design_path = entry.get("design_path") or str(ws.design)
    loop_id = entry.get("loop_id", "")

    paused = status in _PAUSED_STATUSES
    if paused:
        review = _read_plan_review(ws) if isinstance(ws, WorkspacePaths) else None
        if (review or {}).get("state") == "unreadable":
            # Neither approve nor reject can act on a corrupt gate, so don't
            # point the reviewer at buttons that will just error.
            return (
                "## ⛔ AgentLoop 计划闸门文件损坏\n\n"
                f"- **workspace**: `{workspace_dir}`\n"
                f"- **loop_id**: `{loop_id}`\n\n"
                "`plan-review.json` 无法解析，loop 拒绝执行未经批准的计划。"
                "请修复该文件后重新启动，或用 `--fresh` 重新规划。"
                "**不要只删除该文件**——todolist 还在，删掉闸门会让下次启动"
                "直接跑一份没人批准的计划。\n"
            )
        stats = (review or {}).get("stats") or {}
        lines = [
            f"## ⏸️ AgentLoop 等待人工确认（{emoji} {label}）",
            "",
            f"- **计划**: {stats.get('items', 0)} 项"
            f"（dev {stats.get('dev', 0)} / qa {stats.get('qa', 0)}）",
        ]
        unverified = int(stats.get("unverified") or 0)
        if unverified:
            ids = ", ".join(stats.get("unverified_ids") or [])
            lines.append(f"- **⚠️ 无机器检查覆盖**: {unverified} 个 dev item（{ids}）")
        if (review or {}).get("note"):
            lines.append(f"- **备注**: {review['note']}")
        lines += [
            f"- **workspace**: `{workspace_dir}`",
            f"- **design**: `{design_path}`",
            f"- **loop_id**: `{loop_id}`",
            "",
            "在左侧 AgentLoop 面板审阅 `todolist.md` 后点击**批准**即可开始执行；"
            "也可以先直接编辑 todolist，批准时会绑定编辑后的版本。",
        ]
        return "\n".join(lines) + "\n"

    lines = [
        f"## 🎯 AgentLoop 已结束（{emoji} {label}）",
        "",
        f"- **退出原因**: {exhausted_reason}",
        f"- **cycles**: {cycle}",
        f"- **总成本**: ¥{total_cost:.2f}",
        f"- **workspace**: `{workspace_dir}`",
        f"- **design**: `{design_path}`",
        f"- **loop_id**: `{loop_id}` （在左侧 AgentLoop 面板中点击可查看完整流水）",
    ]

    summary = _read_summary_md(ws)
    if summary is None:
        lines += ["", "> summary.md 未生成"]
    else:
        lines += [
            "",
            "<details>",
            "<summary>📄 summary.md</summary>",
            "",
            summary.rstrip(),
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"


async def notify_source_task(loop_id: str) -> bool:
    """Inject a completion system-message into the loop's source agent task.

    Returns ``True`` if a fresh notification was posted, ``False`` if skipped
    (already notified, status not notifiable, no source task, etc.). The
    function is idempotent: every skip path that consumes a chance (no
    source task, task deleted) still stamps ``notified_at`` so we never
    retry. Genuine "not finished yet" cases (running) leave the flag alone.

    Concurrency: a per-loop ``asyncio.Lock`` plus a ``notified_at`` stamp
    written *before* any ``await`` ensures two concurrent triggers (GET
    refresh + 60s sweep, or two simultaneous GETs) cannot both append a
    system-message. Whichever coroutine grabs the lock first claims the
    slot; the second sees ``notified_at`` set and bails.
    """
    async with _get_notify_lock(loop_id):
        entry = _find(loop_id)
        if not entry:
            return False
        if entry.get("notified_at"):
            return False

        status = (entry.get("status") or "").lower()
        if status not in _NOTIFIABLE_STATUSES:
            # ``running`` / ``stopped`` / unrecognised: leave for the next refresh.
            # ``stopped`` is intentional — user asked for it, no need to ping back.
            return False

        source_task_id = entry.get("source_task_id")
        if not source_task_id:
            # No origin recorded — nothing we can deliver to. Stamp so we don't
            # waste cycles re-checking on every poll/refresh.
            _update_fields(loop_id, notified_at=_utcnow())
            return False

        # Imports kept local to avoid a circular at module-load time
        # (agentloop_manager is imported by routes_agentloop, which is imported
        # by main.py before app_state is fully wired).
        from server.state import app_state
        from server.models import Message
        from server.routes_ws import broadcast, task_updated_message

        task = app_state.get_task(source_task_id)
        if task is None:
            # Source task was deleted before the loop finished — stamp and skip.
            _update_fields(loop_id, notified_at=_utcnow())
            return False

        try:
            ws = _entry_workspace(entry)
        except ValueError:
            logger.exception("notify_source_task: cannot resolve workspace for %s", loop_id)
            return False

        try:
            content = _render_completion_message(entry, ws)
        except Exception:  # noqa: BLE001
            logger.exception("notify_source_task: failed to render message for %s", loop_id)
            return False

        # Stamp ``notified_at`` BEFORE any ``await`` so that even if a
        # concurrent trigger somehow bypassed the lock (e.g. across
        # processes via the registry file), the second one would see
        # the stamp and bail out. Combined with the lock above, this
        # gives us strong idempotency under the in-process event loop.
        _update_fields(loop_id, notified_at=_utcnow())

        msg = Message(role="agent", type="system", streaming=False, content=content)
        task.messages.append(msg)
        # Bump the task's updated_at so the UI surfaces this completed
        # task at the top of its agent's task list and the unseen badge
        # logic (driven by updated_at, not just message append) fires
        # for users who don't have the task currently open.
        from server.models import _utcnow as _model_utcnow
        task.updated_at = _model_utcnow()

        try:
            await broadcast({
                "type": "message",
                "task_id": source_task_id,
                "message": msg.model_dump(),
            })
        except Exception:  # noqa: BLE001
            # Broadcast failures shouldn't block persistence — the message is
            # already in memory and will be saved below.
            logger.exception("notify_source_task: broadcast failed for %s", loop_id)

        # Notify list views / sidebar that the task's recency changed so
        # finished loops bubble up even when the source task isn't open.
        try:
            await broadcast(task_updated_message(task))
        except Exception:  # noqa: BLE001
            logger.exception(
                "notify_source_task: task_updated broadcast failed for %s", loop_id
            )

        # Persist the message — this is mandatory for already-finished tasks since
        # the normal save_agent_tasks calls (driven by agent_runner) won't fire.
        try:
            app_state.save_agent_tasks(task.agent_id)
        except Exception:  # noqa: BLE001
            logger.exception("notify_source_task: persist failed for %s", loop_id)

        logger.info(
            "AgentLoop %s notified source task %s (status=%s)",
            loop_id, source_task_id, status,
        )
        return True


async def notify_pending() -> int:
    """Sweep the registry and notify every entry whose status is finalized
    but ``notified_at`` is still empty. Returns the number of fresh
    notifications delivered. Safe to call repeatedly — fully idempotent.
    """
    delivered = 0
    # ``list_all`` invokes ``_refresh_status`` for each entry, so any newly
    # finished loops get their status updated to one of the notifiable values
    # before we read them back here.
    for entry in list_all(include_dismissed=True):
        if entry.get("notified_at"):
            continue
        if (entry.get("status") or "").lower() not in _NOTIFIABLE_STATUSES:
            continue
        try:
            if await notify_source_task(entry["loop_id"]):
                delivered += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "notify_pending: error notifying loop %s", entry.get("loop_id")
            )
    return delivered
