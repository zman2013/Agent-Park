"""Tests for run_agent stall/timeout/normal paths in agents/base.py."""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import pytest

from agentloop.agents.base import run_agent
from agentloop.config import AgentBackend


def _make_fake_cli(tmp_path: Path, name: str, script: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/bash\n" + script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _run_dir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


def test_normal_exit_success(tmp_path: Path) -> None:
    result_line = json.dumps(
        {"type": "result", "subtype": "success", "total_cost_usd": 0.01,
         "num_turns": 1, "result": "hello"}
    )
    cli = _make_fake_cli(
        tmp_path, "fake_cli.sh",
        f'echo \'{result_line}\'\nexit 0\n',
    )
    backend = AgentBackend(cmd=cli, stall_timeout_sec=10, timeout_sec=10)
    res = run_agent("dev", _run_dir(tmp_path), "T-1", backend, "prompt")
    assert res.success is True
    assert res.result_text == "hello"
    assert res.num_turns == 1
    assert res.errors is None


def test_stall_detected_and_killed(tmp_path: Path) -> None:
    # CLI prints one line then sleeps forever → stall should trigger.
    cli = _make_fake_cli(
        tmp_path, "stall_cli.sh",
        'echo \'{"type":"user"}\'\nsleep 300\n',
    )
    backend = AgentBackend(cmd=cli, stall_timeout_sec=2, timeout_sec=60)
    t0 = time.monotonic()
    res = run_agent("qa", _run_dir(tmp_path), "T-1", backend, "prompt")
    elapsed = time.monotonic() - t0
    assert res.success is False
    assert res.errors is not None
    assert any("stalled" in e for e in res.errors)
    # should fire well before the 60s absolute timeout
    assert elapsed < 15, f"stall kill took too long: {elapsed:.1f}s"


def test_absolute_timeout_when_no_stall(tmp_path: Path) -> None:
    # CLI keeps emitting lines fast enough to avoid stall, but never exits.
    cli = _make_fake_cli(
        tmp_path, "busy_cli.sh",
        'while true; do echo \'{"type":"user"}\'; sleep 0.2; done\n',
    )
    backend = AgentBackend(cmd=cli, stall_timeout_sec=10, timeout_sec=2)
    t0 = time.monotonic()
    res = run_agent("dev", _run_dir(tmp_path), None, backend, "prompt")
    elapsed = time.monotonic() - t0
    assert res.success is False
    assert res.errors is not None
    assert any("timeout" in e for e in res.errors)
    assert elapsed < 15


def test_kill_reaches_child_processes(tmp_path: Path) -> None:
    # Parent script spawns a long-lived child and records its pid, then
    # stalls. After stall kill, both parent and child must be gone.
    pid_file = tmp_path / "child.pid"
    cli = _make_fake_cli(
        tmp_path, "child_cli.sh",
        f'sleep 600 &\necho $! > {pid_file}\n'
        'echo \'{"type":"user"}\'\n'
        'wait\n',
    )
    backend = AgentBackend(cmd=cli, stall_timeout_sec=2, timeout_sec=60)
    run_agent("dev", _run_dir(tmp_path), None, backend, "prompt")
    assert pid_file.exists()
    child_pid = int(pid_file.read_text().strip())
    # give the kernel a moment to reap; a zombie (State: Z) also counts as dead
    for _ in range(30):
        status_path = Path(f"/proc/{child_pid}/status")
        if not status_path.exists():
            return
        state_line = next(
            (ln for ln in status_path.read_text().splitlines() if ln.startswith("State:")),
            "",
        )
        if "Z" in state_line:  # zombie — already terminated
            return
        time.sleep(0.1)
    pytest.fail(f"child process {child_pid} survived stall kill")
