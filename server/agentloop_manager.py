"""Manage independent agentloop subprocesses.

agentloop is a separate CLI under ``agentloop/`` that drives a
planner/PM/dev/qa pipeline against a ``design.md`` file inside a target cwd.
Its state is fully persisted on disk (``.agentloop/state.json``,
``todolist.md``, ``.agentloop/runs/*.jsonl``). This module spawns those
processes detached from any agent-park task, tracks them in a registry,
recovers orphans on restart, and serves snapshots to the UI.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
REGISTRY_FILE = DATA_DIR / "agentloops.json"

AGENTLOOP_DIR = ".agentloop"
STATE_FILE = "state.json"
TODOLIST_FILE = "todolist.md"
RUNS_DIR = "runs"
STDOUT_LOG = "stdout.log"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loop_id(cwd: str) -> str:
    return hashlib.sha256(cwd.encode("utf-8")).hexdigest()[:8]


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
    # Non-blocking reap: collects our own child if it has already exited, so
    # subsequent probes see ProcessLookupError. Safe no-op if pid is not our
    # direct child (raises ChildProcessError).
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
        # Process exists under another uid; can't introspect /proc status
        # reliably, assume alive.
        return True

    # Filter zombies via /proc (Linux-only; this project targets Linux per
    # CLAUDE.md). Other platforms fall through and report alive.
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("State:"):
                    # e.g. "State:\tZ (zombie)"
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == "Z":
                        return False
                    break
    except (FileNotFoundError, OSError):
        return False
    return True


def _read_state(cwd: Path) -> dict[str, Any] | None:
    state_path = cwd / AGENTLOOP_DIR / STATE_FILE
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _derive_status_from_state(state: dict[str, Any] | None) -> str:
    """Infer a finished loop's status from its state.json.

    The agentloop CLI (``agentloop/loop.py``) returns ExitCode.SUCCESS without
    setting ``exhausted_reason`` when PM decides ``done`` and every todolist
    item is done (loop.py:120–124). So the primary signal for a successful
    completion is ``last_decision.next == "done"``; ``exhausted_reason`` only
    fires on budget/limit/validation exhaustion.
    """
    if not state:
        return "unknown"
    last = state.get("last_decision") or {}
    if last.get("next") == "done":
        return "done"
    if state.get("exhausted_reason"):
        return "exhausted"
    # Process gone but state shows neither done nor exhausted — likely crash
    # or manual kill.
    return "stopped"


# ── public API ────────────────────────────────────────────────────────────────


def start(
    cwd: str | Path,
    design_path: str | Path | None = None,
    source_task_id: str | None = None,
) -> dict[str, Any]:
    """Spawn an agentloop process detached from the current session.

    Returns the registry entry. The caller should poll ``list_recent`` or
    ``get_snapshot`` for progress.
    """
    cwd_path = Path(str(cwd)).resolve()
    if not cwd_path.is_dir():
        raise ValueError(f"cwd does not exist or is not a directory: {cwd_path}")

    design = Path(str(design_path)) if design_path else cwd_path / "design.md"
    if not design.is_absolute():
        design = (cwd_path / design).resolve()
    if not design.is_file():
        raise ValueError(f"design file not found: {design}")

    loop_id = _loop_id(str(cwd_path))

    # If an entry already exists and its pid is alive, return as-is (idempotent).
    existing = _find(loop_id)
    if existing and existing.get("status") == "running":
        pid = existing.get("pid")
        if pid and _pid_alive(int(pid)):
            logger.info("agentloop already running for %s (pid=%s)", cwd_path, pid)
            return existing

    # agentloop CLI assumes cwd == design.parent (loop.py:54 `cwd = design_path.parent`).
    # When the caller's design sits outside cwd (e.g. /home/.../plans/*.md), the CLI
    # would otherwise resolve state/runs/todolist into the design's directory, not the
    # cwd we registered here. Symlink design into cwd/design.md so both the CLI and
    # planner's `{cwd}/design.md` prompt find the same file.
    design_in_cwd = cwd_path / "design.md"
    target = design.resolve()
    if design_in_cwd.is_symlink():
        if design_in_cwd.resolve() != target:
            raise ValueError(
                f"{design_in_cwd} already exists as a symlink pointing elsewhere; "
                f"refusing to overwrite"
            )
    elif design_in_cwd.exists():
        if design_in_cwd.resolve() != target:
            raise ValueError(
                f"{design_in_cwd} already exists as a real file; "
                f"refusing to overwrite (move or remove it first)"
            )
    else:
        design_in_cwd.symlink_to(target)
    launch_design = design_in_cwd

    (cwd_path / AGENTLOOP_DIR).mkdir(exist_ok=True)
    stdout_log = cwd_path / AGENTLOOP_DIR / STDOUT_LOG
    log_fp = open(stdout_log, "ab", buffering=0)

    # Launch with start_new_session=True so the child lives beyond us.
    # agentloop is importable via `python -m agentloop` from the repo root.
    repo_root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(repo_root) + os.pathsep + os.environ.get("PYTHONPATH", "")}

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agentloop", "run", str(launch_design)],
            cwd=str(cwd_path),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception:
        log_fp.close()
        raise

    entry = {
        "loop_id": loop_id,
        "cwd": str(cwd_path),
        "design_path": str(design),
        "pid": proc.pid,
        "started_at": _utcnow(),
        "source_task_id": source_task_id,
        "status": "running",
        "dismissed": False,
        "last_seen_cycle": 0,
    }
    _upsert(entry)
    logger.info("Started agentloop pid=%s loop_id=%s cwd=%s", proc.pid, loop_id, cwd_path)
    return entry


def stop(loop_id: str, timeout_sec: float = 10.0) -> dict[str, Any] | None:
    entry = _find(loop_id)
    if not entry:
        return None
    pid = int(entry.get("pid") or 0)
    if pid and _pid_alive(pid):
        try:
            # Signal the process group (we used start_new_session=True).
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

        deadline = time.time() + timeout_sec
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.2)

        if _pid_alive(pid):
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


def _refresh_status(entry: dict[str, Any]) -> dict[str, Any]:
    """Reconcile an entry's ``status`` field with actual process/state on disk."""
    pid = int(entry.get("pid") or 0)
    cwd_path = Path(entry["cwd"])
    state = _read_state(cwd_path)

    if entry.get("status") == "running":
        if not _pid_alive(pid):
            derived = _derive_status_from_state(state)
            _update_fields(entry["loop_id"], status=derived, stopped_at=_utcnow())
            entry["status"] = derived

    # keep last_seen_cycle roughly fresh
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
    # newest first by started_at
    result.sort(key=lambda e: e.get("started_at", ""), reverse=True)
    return result


def list_recent(limit: int = 5, days: int = 7) -> list[dict[str, Any]]:
    """Sidebar 'recent updates' list: non-dismissed, within N days, most recent first."""
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
    cwd_path = Path(entry["cwd"])
    summary["state"] = _read_state(cwd_path)
    summary["todolist"] = _read_todolist(cwd_path)
    summary["runs"] = _list_runs(cwd_path)
    return summary


def get_run_log(loop_id: str, cycle: int) -> list[dict[str, Any]] | None:
    entry = _find(loop_id)
    if not entry:
        return None
    cwd_path = Path(entry["cwd"])
    runs_dir = cwd_path / AGENTLOOP_DIR / RUNS_DIR
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
        if _pid_alive(pid):
            logger.info("Re-claimed orphan agentloop pid=%s loop_id=%s", pid, loop_id)
            continue
        state = _read_state(Path(entry["cwd"]))
        derived = _derive_status_from_state(state)
        _update_fields(loop_id, status=derived, stopped_at=_utcnow())
        results.append(loop_id)
    return results


# ── helpers ──────────────────────────────────────────────────────────────────


def _summary(entry: dict[str, Any]) -> dict[str, Any]:
    """Shallow-copy the entry and tack on a small state summary."""
    out = dict(entry)
    cwd_path = Path(entry["cwd"])
    state = _read_state(cwd_path)
    if state:
        out["cycle"] = int(state.get("cycle", 0))
        out["total_cost_cny"] = float(state.get("total_cost_cny", 0.0))
        out["exhausted_reason"] = state.get("exhausted_reason")
    else:
        out["cycle"] = 0
        out["total_cost_cny"] = 0.0
        out["exhausted_reason"] = None
    out["cwd_basename"] = cwd_path.name
    return out


def _read_todolist(cwd: Path) -> dict[str, Any]:
    """Parse todolist.md using agentloop's own parser, returning a JSON dict."""
    try:
        from agentloop.todolist import parse as parse_todolist
    except ImportError:
        return {"metadata": {}, "items": []}
    try:
        tl = parse_todolist(cwd)
    except Exception:
        logger.exception("Failed to parse todolist for %s", cwd)
        return {"metadata": {}, "items": []}
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
    return {"metadata": dict(tl.metadata), "items": items}


def _list_runs(cwd: Path) -> list[dict[str, Any]]:
    runs_dir = cwd / AGENTLOOP_DIR / RUNS_DIR
    if not runs_dir.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for p in sorted(runs_dir.iterdir()):
        name = p.name
        if not name.endswith(".jsonl"):
            continue
        # filename format: NNN-<actor>[-<item_id>].jsonl
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
