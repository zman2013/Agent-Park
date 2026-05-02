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

import hashlib
import json
import logging
import os
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


def _derive_status_from_state(state: dict[str, Any] | None) -> str:
    """Infer a finished loop's status from its state.json."""
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


def _refresh_status(entry: dict[str, Any]) -> dict[str, Any]:
    """Reconcile an entry's ``status`` field with actual process/state on disk."""
    pid = int(entry.get("pid") or 0)
    expected_st = entry.get("pid_start_time")
    ws = _entry_workspace(entry)
    state = _read_state(ws)

    if entry.get("status") == "running":
        if not _pid_matches(pid, expected_st):
            derived = _derive_status_from_state(state)
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
        derived = _derive_status_from_state(state)
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
    # Coarse but sufficient guard — if the user already wrote a feishu
    # section we don't want to double-inject or contradict their edits.
    if "[summary.feishu]" in existing:
        return

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
    return out


def _read_todolist(ws: WorkspacePaths) -> dict[str, Any]:
    """Parse todolist.md using agentloop's own parser, returning a JSON dict."""
    try:
        from agentloop.todolist import parse as parse_todolist
    except ImportError:
        return {"metadata": {}, "items": []}
    try:
        tl = parse_todolist(ws)
    except Exception:
        logger.exception("Failed to parse todolist for %s", ws.todolist)
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
