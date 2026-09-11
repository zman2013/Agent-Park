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
import sys
import time
from pathlib import Path

from server import agent_runner as agent_runner_mod
from server.adapters.codex import CodexAdapter
from server.agent_runner import AgentRunner, _pgroup_members, _read_proc_start_time
from server.models import Agent, Task, TaskStatus
from server.state import app_state


def _descendants(pid):
    """Every transitive child of *pid*, read from ps rather than /proc walks."""
    out = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True
    ).stdout
    kids = [int(x) for x in out.split()]
    return kids + [g for k in kids for g in _descendants(k)]


def _identity(pid):
    """(pid, starttime) — a pid alone is not a stable process identity.

    These tests kill processes and then assert on the result, which is exactly
    when the kernel is free to hand the number to something else. Comparing the
    start-time field too means a recycled pid reads as dead (different identity)
    rather than as a survivor.
    """
    return (pid, _read_proc_start_time(pid))


def _alive_as(pid, identity):
    """True only if *pid* is running AND is still the same process as *identity*."""
    return _alive(pid) and _identity(pid) == identity


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


def _stubborn_wrapper(tmp_path):
    """A wrapper that dies instantly on SIGTERM while its child ignores it.

    This is the shape proc.returncode cannot see: the direct child is gone
    (`exec sleep` takes SIGTERM immediately), yet the descendant holding the
    writer lock installed SIG_IGN and is only removable by SIGKILL. The child is
    a bare interpreter with no children of its own so the fixture proves the
    escalation, not a cascade of collateral kills.
    """
    middle = tmp_path / "stubborn.sh"
    middle.write_text(
        "#!/bin/bash\n"
        'echo \'{"type":"thread.started","thread_id":"t-test"}\'\n'
        f"{sys.executable} -c 'import signal,time;"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)' &\n"
        "exec sleep 300\n"
    )
    middle.chmod(0o755)
    return middle


def _eof_then_stubborn_wrapper(tmp_path):
    """Wrapper dies on SIGTERM; its SIGTERM-ignoring descendant keeps running.

    The descendant redirects its own stdout to /dev/null, so the wrapper's exit
    is enough to close the pipe: _run_pipe_mode hits EOF, returns, and the
    _live_runs entry is dropped — while the process itself lives on. That is how
    a group gets forgotten between shutdown()'s SIGTERM and SIGKILL phases.
    """
    script = tmp_path / "eof_stubborn.sh"
    script.write_text(
        "#!/bin/bash\n"
        'echo \'{"type":"thread.started","thread_id":"t-test"}\'\n'
        f"{sys.executable} -c 'import signal,time;"
        " signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'"
        " >/dev/null 2>&1 &\n"
        # exec so SIGTERM lands on this pid directly with no bash trap handling.
        "exec sleep 300\n"
    )
    script.chmod(0o755)
    return script


class _Ctx:
    """_run_pipe_mode only reads .task_id on the paths these tests reach.

    save_session is a no-op: the fixture emits a thread.started line so the
    reader has something to parse, and the adapter persists the session id from
    it. Writing it would touch the live data/ directory.
    """

    def __init__(self, task_id):
        self.task_id = task_id

    async def save_session(self, *a, **k):
        pass


async def _spawn(monkeypatch, tmp_path, wrapper=_wrapper):
    """Start a real pipe-mode run and return (runner, task, proc, run_id)."""
    agent = Agent(name="killtest", command=str(wrapper(tmp_path)), cwd=str(tmp_path))
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


async def _check_child_leads_its_own_process_group(monkeypatch, tmp_path):
    runner, task, proc, reader = await _spawn(monkeypatch, tmp_path)
    try:
        assert os.getpgid(proc.pid) == proc.pid
    finally:
        await runner.kill_task(task.id)
        reader.cancel()


def test_pipe_mode_child_leads_its_own_process_group(monkeypatch, tmp_path):
    """start_new_session makes proc.pid a pgid, which is what killpg needs."""
    asyncio.run(_check_child_leads_its_own_process_group(monkeypatch, tmp_path))


async def _check_kill_task_reaps_grandchildren(monkeypatch, tmp_path):
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


def test_kill_task_reaps_grandchildren(monkeypatch, tmp_path):
    """The whole tree dies, not just the process asyncio holds a handle to."""
    asyncio.run(_check_kill_task_reaps_grandchildren(monkeypatch, tmp_path))


async def _check_pid_is_recorded(monkeypatch, tmp_path):
    runner, task, proc, reader = await _spawn(monkeypatch, tmp_path)
    try:
        assert runner._live_runs["run-killtest"]["pid"] == proc.pid
        assert getattr(task, "subprocess_pid", None) == proc.pid
        assert getattr(task, "subprocess_start_time", None) is not None
    finally:
        await runner.kill_task(task.id)
        reader.cancel()


def test_pipe_mode_pid_is_recorded_for_killpg_and_orphan_recovery(
    monkeypatch, tmp_path
):
    """shutdown() and restore_orphan_tasks() both signal by recorded pid.

    Neither can reach a pipe-mode tree without one: shutdown() would fall back
    to proc.terminate(), and orphan recovery would not even see the process.
    """
    asyncio.run(_check_pid_is_recorded(monkeypatch, tmp_path))


async def _check_sigkill_escalates_on_the_group(monkeypatch, tmp_path):
    runner, task, proc, reader = await _spawn(
        monkeypatch, tmp_path, wrapper=_stubborn_wrapper
    )
    stubborn = [p for p in _descendants(proc.pid) if _alive(p)]
    assert stubborn, "fixture spawned no SIGTERM-ignoring child"
    try:
        await runner.kill_task(task.id)
        await asyncio.sleep(0.6)
        assert [p for p in stubborn if _alive(p)] == []
    finally:
        for p in [proc.pid] + stubborn:
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        reader.cancel()


def test_sigkill_escalates_on_the_group_not_the_wrapper(monkeypatch, tmp_path):
    """A dead wrapper must not cancel SIGKILL while the group is still alive.

    Gating escalation on proc.returncode leaves exactly the writer-lock holder
    this change exists to remove: the wrapper reports exited, so SIGKILL is
    skipped, and the SIGTERM-ignoring descendant survives.
    """
    asyncio.run(_check_sigkill_escalates_on_the_group(monkeypatch, tmp_path))


def _live_group_with_stubborn_worker():
    """A live setsid leader whose worker ignores SIGTERM.

    The identity-verified branch of restore_orphan_tasks(): recorded start time
    matches, so it signals the group and then clears subprocess_pid. Returns
    (pgid, worker_pid, leader_popen).
    """
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,subprocess,sys,time\n"
            "p = subprocess.Popen([sys.executable, '-c',"
            " 'import signal,time;"
            " signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'])\n"
            "print(os.getpid(), p.pid, flush=True)\n"
            "time.sleep(300)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid, worker = (int(x) for x in leader.stdout.readline().split())
    assert _alive(pgid) and _alive(worker)
    assert os.getpgid(worker) == pgid
    return pgid, worker, leader


def test_orphan_recovery_escalates_before_discarding_the_pid(monkeypatch, tmp_path):
    """SIGTERM alone leaks the writer-lock holder along with its only handle.

    restore_orphan_tasks() clears subprocess_pid unconditionally, so this is the
    last moment the group is identifiable. A worker that ignores SIGTERM must be
    SIGKILLed here or it keeps codex's lock forever, unfindable.
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    worker_id = _identity(worker)
    agent = Agent(name="escalatetest", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="escalatetest")
    task.status = TaskStatus.running
    object.__setattr__(task, "subprocess_pid", pgid)
    # Real recorded identity, so the verified branch is the one taken.
    object.__setattr__(task, "subprocess_start_time", _read_proc_start_time(pgid))
    app_state.agents[agent.id] = agent
    # See the isolation note in the leaderless-group test: never add to the real
    # registry, restore_orphan_tasks signals every running task it finds.
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    try:
        assert AgentRunner().restore_orphan_tasks() == [task.id]
        for _ in range(30):
            if not _alive_as(worker, worker_id):
                break
            time.sleep(0.1)
        assert not _alive_as(worker, worker_id), (
            "SIGTERM-ignoring worker outlived its own metadata"
        )
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
        app_state.agents.pop(agent.id, None)


def test_unreadable_stat_is_not_treated_as_leader_absence(monkeypatch, tmp_path):
    """A transient stat failure must not authorize signaling a recycled group.

    _read_proc_start_time returns None both when the pid is gone and when its
    stat cannot be read. Gating the fallback on the latter would let a live,
    unrelated setsid leader (whose pgid equals the recycled pid) be killed.
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    worker_id, leader_id = _identity(worker), _identity(pgid)
    agent = Agent(name="unreadable", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="unreadable")
    task.status = TaskStatus.running
    object.__setattr__(task, "subprocess_pid", pgid)
    object.__setattr__(task, "subprocess_start_time", 999999)  # mismatch
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    # Simulate the transient failure: pid exists, its start time is unreadable.
    # Patched after the identities above are captured, since they use it too.
    monkeypatch.setattr(agent_runner_mod, "_read_proc_start_time", lambda _p: None)
    try:
        assert AgentRunner().restore_orphan_tasks() == [task.id]
        # Give a wrongly-issued kill time to land before concluding it was not
        # issued. Asserting immediately would pass on timing luck alone: the
        # signal is delivered asynchronously, so a freshly-signaled process
        # still reads as running for a moment.
        time.sleep(0.5)
        assert _alive_as(worker, worker_id), (
            "unrelated group killed on an unreadable stat"
        )
        assert _alive_as(pgid, leader_id), (
            "unrelated leader killed on an unreadable stat"
        )
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
        app_state.agents.pop(agent.id, None)


def _leaderless_group():
    """A setsid leader that exits, leaving a live child still in its group.

    This is the shape restore_orphan_tasks() sees after a backend restart: the
    persisted pid is the group leader, /proc no longer has it, but the writer-
    lock holder is still in the group. Returns (pgid, survivor_pid).
    """
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,subprocess,sys,time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
            "print(os.getpid(), p.pid, flush=True)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid, survivor = (int(x) for x in leader.stdout.readline().split())
    leader.wait(timeout=10)
    # Reaped by Popen.wait, so /proc/<pgid> is fully gone — not a zombie.
    for _ in range(50):
        if not Path(f"/proc/{pgid}").exists():
            break
        time.sleep(0.1)
    assert not Path(f"/proc/{pgid}").exists(), "leader did not disappear"
    assert _alive(survivor), "survivor died with its leader"
    assert os.getpgid(survivor) == pgid
    return pgid, survivor


def test_orphan_recovery_kills_survivors_of_a_leaderless_group(monkeypatch, tmp_path):
    """A dead leader must not make its surviving group unrecoverable.

    _read_proc_start_time(pid) returns None once the leader is reaped, so the
    identity check used to skip killpg entirely and clear the pid — abandoning
    exactly the writer-lock holder this PR exists to remove.
    """
    pgid, survivor = _leaderless_group()
    survivor_id = _identity(survivor)
    agent = Agent(name="orphantest", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="orphantest")
    task.status = TaskStatus.running
    object.__setattr__(task, "subprocess_pid", pgid)
    object.__setattr__(task, "subprocess_start_time", 12345)  # stale on purpose
    app_state.agents[agent.id] = agent
    # Replace the task registry outright rather than adding to it. app_state
    # loads every persisted task from data/ at import, and restore_orphan_tasks
    # killpg's the recorded pid of EVERY running/waiting task it finds — running
    # this test against the real registry would kill whatever live agent tasks
    # the machine happens to be running, this test process's own session
    # included. monkeypatch restores the real dict at teardown.
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    try:
        cleaned = AgentRunner().restore_orphan_tasks()
        assert cleaned == [task.id]
        for _ in range(30):
            if not _alive_as(survivor, survivor_id):
                break
            time.sleep(0.1)
        assert not _alive_as(survivor, survivor_id), (
            "survivor of a leaderless group was abandoned"
        )
    finally:
        try:
            os.kill(survivor, 9)
        except ProcessLookupError:
            pass
        app_state.agents.pop(agent.id, None)


async def _check_shutdown_escalates_a_forgotten_group(monkeypatch, tmp_path):
    """A group whose _live_runs entry is gone must still be SIGKILLed.

    _run_pipe_mode returns on stdout EOF, and a descendant can cause that EOF
    just by closing stdout while it keeps running — so _on_done drops the entry
    before shutdown()'s SIGKILL phase and the group used to be forgotten.
    """
    runner, task, proc, reader = await _spawn(
        monkeypatch, tmp_path, wrapper=_eof_then_stubborn_wrapper
    )
    # Find it by pgid, not by walking children: the wrapper exits immediately so
    # the descendant is reparented to init and pgrep -P finds nothing. Staying in
    # the group after losing its parent is the whole property under test.
    stubborn = [p for p in _pgroup_members(proc.pid) if p != proc.pid]
    assert stubborn, "fixture spawned no SIGTERM-ignoring child"
    # _spawn registers the _live_runs entry by hand (it calls _run_pipe_mode
    # directly, not _start_subprocess), so the real _on_done is not attached.
    # Attach the one part that matters: dropping the entry when the reader
    # completes. The entry must still be present when shutdown() starts — that
    # is how the pid gets recorded — and disappear during the drain, which is
    # what used to lose the group before the SIGKILL phase.
    reader.add_done_callback(lambda _f: runner._live_runs.pop("run-killtest", None))
    # shutdown() drains on run["task"]; without it the SIGTERM loop exits at once
    # and never gives the wrapper time to die (which is what drops the entry).
    runner._live_runs["run-killtest"]["task"] = reader
    assert "run-killtest" in runner._live_runs
    assert not reader.done(), "wrapper died before shutdown; fixture proves nothing"
    try:
        await runner.shutdown()
        # The entry is gone (wrapper took SIGTERM, its exit closed stdout, the
        # reader finished, the callback fired) — so only the independent pgid
        # tracking can still reach the descendant.
        assert not runner._live_runs, "entry survived; the bug is not reproduced"
        await asyncio.sleep(0.5)
        assert [p for p in stubborn if _alive(p)] == []
    finally:
        for p in [proc.pid] + stubborn:
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass


def test_shutdown_escalates_a_group_whose_run_entry_is_gone(monkeypatch, tmp_path):
    asyncio.run(_check_shutdown_escalates_a_forgotten_group(monkeypatch, tmp_path))
