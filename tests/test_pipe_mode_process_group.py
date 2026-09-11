"""Pipe mode must kill the whole process group, not just its direct child.

Agent commands are wrapper scripts: `codexgpt` execs `ept codex`, which spawns a
node launcher, which spawns the real binary. proc.terminate() reaches only the
outermost process, so a grandchild kept running after kill_task — and a
surviving codex process holds that thread's writer lock, making every later
`codex exec resume` fail with "thread ... already has an active writer". The
task then cannot be continued at all; observed on a real task that had run for
15h before its wrapper died and left two orphans behind.

The PTY path never had this problem: its child calls os.setsid(), so the
recorded pid is a process-group leader and killpg reaches the whole tree. These
tests pin the same property for pipe mode.
"""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from server.adapters.codex import CodexAdapter
from server.agent_runner import AgentRunner
from server.models import Agent, Task
from server.state import app_state


def _descendants(pid):
    """Every transitive child of *pid*, read from ps rather than /proc walks."""
    out = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True
    ).stdout
    kids = [int(x) for x in out.split()]
    return kids + [g for k in kids for g in _descendants(k)]


def _alive(pid):
    """Running, not merely present.

    os.kill(pid, 0) is not enough: a reaped-but-not-yet-collected zombie still
    accepts signal 0, so a correctly killed grandchild reads as a survivor.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return False
    # The state letter follows the parenthesised comm, which may itself contain
    # spaces or parens — split on the last ')'.
    return stat.rpartition(")")[2].split()[0] not in ("Z", "X", "x")


def _wrapper(tmp_path):
    """outer execs middle, middle spawns the long-lived worker.

    One JSONL line first so _run_pipe_mode's reader has something to parse and
    the spawn is observably complete.
    """
    middle = tmp_path / "middle.sh"
    middle.write_text(
        "#!/bin/bash\n"
        'echo \'{"type":"thread.started","thread_id":"t-test"}\'\n'
        "sleep 300 &\n"
        "wait\n"
    )
    middle.chmod(0o755)
    outer = tmp_path / "outer.sh"
    outer.write_text(f"#!/bin/bash\nexec {middle}\n")
    outer.chmod(0o755)
    return outer


class _Ctx:
    """_run_pipe_mode only reads .task_id on the paths these tests reach."""

    def __init__(self, task_id):
        self.task_id = task_id


async def _spawn(monkeypatch, tmp_path):
    """Start a real pipe-mode run and return (runner, task, proc, run_id)."""
    agent = Agent(name="killtest", command=str(_wrapper(tmp_path)), cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="killtest")
    app_state.agents[agent.id] = agent
    app_state.tasks[task.id] = task
    # _run_pipe_mode persists the pid for orphan recovery; keep that write out
    # of the live data/ directory. These tests assert on the in-memory Task.
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)

    runner = AgentRunner()
    run_id = "run-killtest"
    runner._live_runs[run_id] = {}
    # Make this run a non-owner so exiting does not run _finish_task, which
    # would send a Feishu card and append to the memory history.
    runner._run_ids[task.id] = "another-run"

    reader = asyncio.create_task(
        runner._run_pipe_mode(
            task.id, [str(agent.command)], str(tmp_path),
            CodexAdapter(), _Ctx(task.id), run_id,
        )
    )
    for _ in range(50):
        await asyncio.sleep(0.1)
        if task.id in runner._async_procs:
            break
    proc = runner._async_procs.get(task.id)
    assert proc is not None, "subprocess was never registered"
    await asyncio.sleep(0.6)  # let the grandchild appear
    return runner, task, proc, reader


@pytest.mark.asyncio
async def test_pipe_mode_child_leads_its_own_process_group(monkeypatch, tmp_path):
    """start_new_session makes proc.pid a pgid, which is what killpg needs."""
    runner, task, proc, reader = await _spawn(monkeypatch, tmp_path)
    try:
        assert os.getpgid(proc.pid) == proc.pid
    finally:
        await runner.kill_task(task.id)
        reader.cancel()


@pytest.mark.asyncio
async def test_kill_task_reaps_grandchildren(monkeypatch, tmp_path):
    """The whole tree dies, not just the process asyncio holds a handle to."""
    runner, task, proc, reader = await _spawn(monkeypatch, tmp_path)
    tree = [proc.pid] + _descendants(proc.pid)
    assert len(tree) >= 2, "fixture spawned no grandchild; test would prove nothing"
    try:
        await runner.kill_task(task.id)
        await asyncio.sleep(0.6)
        assert [p for p in tree if _alive(p)] == []
    finally:
        for p in tree:
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        reader.cancel()


@pytest.mark.asyncio
async def test_pipe_mode_pid_is_recorded_for_killpg_and_orphan_recovery(
    monkeypatch, tmp_path
):
    """shutdown() and restore_orphan_tasks() both signal by recorded pid.

    Neither can reach a pipe-mode tree without one: shutdown() would fall back
    to proc.terminate(), and orphan recovery would not even see the process.
    """
    runner, task, proc, reader = await _spawn(monkeypatch, tmp_path)
    try:
        assert runner._live_runs["run-killtest"]["pid"] == proc.pid
        assert getattr(task, "subprocess_pid", None) == proc.pid
        assert getattr(task, "subprocess_start_time", None) is not None
    finally:
        await runner.kill_task(task.id)
        reader.cancel()
