"""Base agent runner.

Spawns a ``cco`` / ``ccs`` / ... subprocess in stream-json mode, writes the
raw stream to ``.agentloop/runs/<NNN>-<role>[-<item_id>].jsonl``, and returns
aggregated results (final text, cost, success).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import AgentBackend

logger = logging.getLogger(__name__)

# rough USD→CNY conversion for cost display (no live FX lookup; MVP)
USD_TO_CNY = 7.2


@dataclass
class RunResult:
    stream_json_path: Path
    duration_sec: float
    cost_cny: float
    success: bool
    result_text: str = ""
    errors: list[str] | None = None
    num_turns: int = 0


def run_agent(
    role: str,
    cwd: Path,
    item_id: str | None,
    backend: AgentBackend,
    prompt: str,
) -> RunResult:
    """Spawn the backend CLI and capture its stream-json output.

    ``role`` / ``item_id`` are used only for log filenames and (optional)
    telemetry; the actual instructions come from ``prompt``.
    """
    if backend.cmd is None:
        raise ValueError(f"role {role!r} has no backend cmd (code-version?)")

    runs_dir = cwd / ".agentloop" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    seq = _next_sequence(runs_dir)
    suffix = f"-{item_id}" if item_id else ""
    log_path = runs_dir / f"{seq:03d}-{role}{suffix}.jsonl"

    args = [backend.cmd, "-p", "--output-format", "stream-json", "--verbose"]
    if backend.model:
        args += ["--model", backend.model]
    args.append(prompt)

    t0 = time.monotonic()
    logger.info("running %s (role=%s item=%s) — log=%s", backend.cmd, role, item_id, log_path)

    collected: list[str] = []
    stall_timeout = max(0, backend.stall_timeout_sec)
    abs_timeout = max(0, backend.timeout_sec)

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line-buffered
            start_new_session=True,  # so we can killpg the whole tree
        )
    except FileNotFoundError as e:
        return RunResult(
            stream_json_path=log_path,
            duration_sec=time.monotonic() - t0,
            cost_cny=0.0,
            success=False,
            errors=[f"command not found: {backend.cmd} ({e})"],
        )

    # Read stdout on a background thread; the main thread polls for stall /
    # absolute timeout so the CLI can't wedge us by simply not writing.
    line_q: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                line_q.put(line)
        finally:
            line_q.put(None)  # EOF sentinel

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    kill_reason: str | None = None
    last_activity = time.monotonic()
    eof = False

    try:
        with open(log_path, "w", encoding="utf-8") as log_fp:
            while not eof:
                now = time.monotonic()
                if abs_timeout and now - t0 > abs_timeout:
                    kill_reason = f"timeout after {abs_timeout}s"
                    break
                if stall_timeout and now - last_activity > stall_timeout:
                    kill_reason = f"stalled: no output for {stall_timeout}s"
                    break
                try:
                    item = line_q.get(timeout=1.0)
                except queue.Empty:
                    continue
                if item is None:
                    eof = True
                    break
                log_fp.write(item)
                log_fp.flush()
                collected.append(item)
                last_activity = time.monotonic()
    finally:
        if kill_reason is not None:
            logger.error("%s %s — killing process group", backend.cmd, kill_reason)
            _kill_process_tree(proc)
            returncode = proc.wait()
        else:
            try:
                returncode = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                returncode = proc.wait()
        reader.join(timeout=2.0)

    duration = time.monotonic() - t0

    if kill_reason is not None:
        return RunResult(
            stream_json_path=log_path,
            duration_sec=duration,
            cost_cny=_parse_stream_json(collected).cost_usd * USD_TO_CNY,
            success=False,
            errors=[kill_reason],
        )

    aggregate = _parse_stream_json(collected)
    success = returncode == 0 and not aggregate.is_error
    errors = aggregate.errors if not success else None
    if returncode != 0 and not errors:
        errors = [f"exit code {returncode}"]

    return RunResult(
        stream_json_path=log_path,
        duration_sec=duration,
        cost_cny=aggregate.cost_usd * USD_TO_CNY,
        success=success,
        result_text=aggregate.result_text,
        errors=errors,
        num_turns=aggregate.num_turns,
    )


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill the entire process group spawned by ``proc``.

    The CLI wrappers (``ept`` → ``node``) live in the same session created
    via ``start_new_session=True``; ``killpg`` reaches all of them. Fall back
    to killing just the top process if the PG call fails.
    """
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            return
    # brief grace period before SIGKILL
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# ----- stream-json parsing ------------------------------------------------


@dataclass
class _Aggregate:
    result_text: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    is_error: bool = False
    errors: list[str] | None = None


def _parse_stream_json(lines: list[str]) -> _Aggregate:
    agg = _Aggregate()
    text_parts: list[str] = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        chunk_type = obj.get("type")
        if chunk_type == "result":
            agg.cost_usd = float(obj.get("total_cost_usd") or 0)
            agg.num_turns = int(obj.get("num_turns") or 0)
            subtype = obj.get("subtype", "")
            is_error = bool(obj.get("is_error")) or subtype in ("error", "error_during_execution")
            agg.is_error = is_error
            result_text = obj.get("result", "")
            if isinstance(result_text, str) and result_text:
                agg.result_text = result_text
            raw_errors = obj.get("errors") or []
            if is_error and raw_errors:
                agg.errors = [str(e) for e in raw_errors]
        elif chunk_type == "stream_event":
            event = obj.get("event", {})
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_parts.append(delta.get("text", ""))
    if not agg.result_text and text_parts:
        agg.result_text = "".join(text_parts)
    return agg


def _next_sequence(runs_dir: Path) -> int:
    # Sort numerically, not lexicographically — otherwise once the sequence
    # crosses 1000 we get "1000-*" < "999-*" and _next_sequence would keep
    # returning a stale value, overwriting older run logs.
    max_seen = 0
    for p in runs_dir.glob("*.jsonl"):
        prefix = p.name.split("-", 1)[0]
        try:
            n = int(prefix)
        except ValueError:
            continue
        if n > max_seen:
            max_seen = n
    return max_seen + 1
