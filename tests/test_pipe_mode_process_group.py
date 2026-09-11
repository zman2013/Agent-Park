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
import signal
import subprocess
import sys
import time
from pathlib import Path

from server import agent_runner as agent_runner_mod
from server.adapters.codex import CodexAdapter
from server.agent_runner import AgentRunner, _pgroup_members, _read_proc_start_time
from server.models import Agent, Task, TaskStatus
from server.state import app_state

# 真实探针的引用，供需要"先打桩再恢复"的测试用（monkeypatch.undo 会连
# save_agent_tasks 的桩一起撤掉，那会写进真实 data/ 目录）。
_real_pid_is_absent = agent_runner_mod._pid_is_absent


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


def _live_group_with_stubborn_worker(stubborn=True):
    """A live setsid leader whose worker ignores SIGTERM.

    The identity-verified branch of restore_orphan_tasks(): recorded start time
    matches, so it signals the group and then clears subprocess_pid. Returns
    (pgid, worker_pid, leader_popen).

    stubborn=False gives the worker default SIGTERM handling — for tests that
    only need to observe *whether* a group was signaled, not the escalation.
    """
    worker_body = "import signal,time; "
    if stubborn:
        worker_body += "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    worker_body += "time.sleep(300)"
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,subprocess,sys,time\n"
            "p = subprocess.Popen([sys.executable, '-c',"
            f" {worker_body!r}])\n"
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


def _live_group_with_graceful_worker(marker, delay=0.6):
    """A live setsid leader whose worker exits on SIGTERM, but not instantly.

    The worker takes *delay* seconds to shut down cleanly and touches *marker* on
    its way out; a SIGTERM arriving inside that window leaves no marker. That is
    the observable difference between "shutdown gave the group a graceful TERM
    interval" and "shutdown escalated in the same loop turn".

    SIG_IGN is installed at handler entry so the repeated SIGTERMs of shutdown's
    drain cannot re-enter the handler. Returns (pgid, worker_pid, leader_popen).

    Readiness is a handshake FROM THE WORKER, not the leader's pid print: on a
    scheduler where the parent resumes right after Popen, the pid line can be
    printed before the worker has reached signal.signal(). shutdown() would then
    deliver SIGTERM under the default disposition, the worker would die instantly
    without touching *marker*, and the test would fail on timing alone. The worker
    writes a ready file as its LAST step before sleeping, so waiting for that file
    proves the handler is installed.
    """
    ready = Path(str(marker) + ".worker-ready")
    worker_src = (
        "import os,signal,sys,time\n"
        "def _bye(*_):\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    time.sleep({delay})\n"
        f"    open({str(marker)!r}, 'w').close()\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, _bye)\n"
        # Handler installed — only now announce readiness. os.replace so the
        # parent can never observe a half-created file.
        f"tmp = {str(ready)!r} + '.tmp'\n"
        "open(tmp, 'w').close()\n"
        f"os.replace(tmp, {str(ready)!r})\n"
        "time.sleep(300)\n"
    )
    leader = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,subprocess,sys,time\n"
            f"p = subprocess.Popen([sys.executable, '-c', {worker_src!r}])\n"
            "print(os.getpid(), p.pid, flush=True)\n"
            "time.sleep(300)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    pgid, worker = (int(x) for x in leader.stdout.readline().split())
    for _ in range(100):
        if ready.exists():
            break
        time.sleep(0.05)
    assert ready.exists(), "worker never installed its SIGTERM handler"
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


def test_incomplete_proc_scan_is_unknown_not_gone(monkeypatch):
    """An unreadable /proc cannot prove a group is empty.

    _scan_pgroup returning [] on a listdir/stat failure used to read as "no
    members", so a verified-identity group was classified gone — stopping the
    escalation and releasing the metadata of a possibly live group.
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    real_start = _read_proc_start_time(pgid)
    try:
        assert agent_runner_mod._group_state(pgid, real_start) == (
            agent_runner_mod._GROUP_OURS
        )
        # Now the whole listing fails: same identity, but nothing is knowable.
        monkeypatch.setattr(
            agent_runner_mod.os, "listdir", lambda _p: (_ for _ in ()).throw(OSError())
        )
        members, complete = agent_runner_mod._scan_pgroup(pgid)
        assert (members, complete) == ([], False)
        assert agent_runner_mod._group_state(pgid, real_start) == (
            agent_runner_mod._GROUP_UNKNOWN
        )
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)


def test_group_state_rechecks_absence_after_enumeration(monkeypatch):
    """The centralized gateway needs the same post-scan absence recheck.

    A pid absent at the identity checks can be re-leased before the member scan;
    the new session leader's group then answers to this pgid and would be
    classified ours, which _killpg_verified would happily signal.
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    calls = {"n": 0}

    def _absent_once(_pid):
        calls["n"] += 1
        return calls["n"] == 1  # absent at the checks, held by the recheck

    monkeypatch.setattr(agent_runner_mod, "_pid_is_absent", _absent_once)
    monkeypatch.setattr(agent_runner_mod, "_read_proc_start_time", lambda _p: None)
    try:
        state = agent_runner_mod._group_state(pgid, 12345)
        assert state == agent_runner_mod._GROUP_FOREIGN, state
        assert not agent_runner_mod._killpg_verified(pgid, 12345, signal.SIGKILL)
        time.sleep(0.3)
        assert _alive(worker), "signaled a group whose pgid was re-leased"
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)


def test_unreadable_initial_identity_retains_pid(monkeypatch, tmp_path):
    """The initial mismatch branch bypasses _group_state, so it needs its own gate.

    A transient None on the very first read used to fall through to "identity
    check failed", which cleared subprocess_pid — leaving the failed-task retry
    with no pid to revisit and the writer lock held permanently.
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    agent = Agent(name="unreadable-initial", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="unreadable-initial")
    task.status = TaskStatus.running
    object.__setattr__(task, "subprocess_pid", pgid)
    object.__setattr__(task, "subprocess_start_time", _read_proc_start_time(pgid))
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    # Every read fails, so the very first identity check is unverifiable while
    # the pid is provably present.
    monkeypatch.setattr(agent_runner_mod, "_read_proc_start_time", lambda _p: None)
    try:
        assert AgentRunner().restore_orphan_tasks() == [task.id]
        assert getattr(task, "subprocess_pid", None) == pgid, (
            "pid cleared on an unreadable initial identity read"
        )
        assert getattr(task, "subprocess_start_time", None) is not None
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
        app_state.agents.pop(agent.id, None)


def test_unverifiable_group_is_not_reported_as_gone(monkeypatch):
    """"Don't signal" and "it's gone" must be separate verdicts.

    Callers read the gate both ways: kill_task breaks out of its escalation,
    shutdown drops the pgid, and orphan recovery clears the persisted pid. If an
    unreadable stat collapsed to "gone", a SIGTERM-resistant descendant would
    survive while its only recovery metadata was discarded.
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    try:
        monkeypatch.setattr(agent_runner_mod, "_read_proc_start_time", lambda _p: None)
        state = agent_runner_mod._group_state(pgid, 12345)
        assert state == agent_runner_mod._GROUP_UNKNOWN, state
        # Not signalable...
        assert not agent_runner_mod._group_is_still(pgid, 12345)
        # ...but emphatically not gone either.
        assert state != agent_runner_mod._GROUP_GONE
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)


def test_orphan_recovery_retains_pid_when_group_is_unverifiable(
    monkeypatch, tmp_path
):
    """An unverifiable group must keep its metadata, not have it discarded.

    A stale pid costs one extra check next boot; a discarded live one costs a
    permanent writer lock.
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    agent = Agent(name="unverifiable", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="unverifiable")
    task.status = TaskStatus.running
    object.__setattr__(task, "subprocess_pid", pgid)
    real_start = _read_proc_start_time(pgid)
    object.__setattr__(task, "subprocess_start_time", real_start)
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    # Identity verifies on the way in, then becomes unreadable — the group's fate
    # is unknown at the moment the metadata would be released.
    calls = {"n": 0}

    def _flaky(p):
        calls["n"] += 1
        return real_start if calls["n"] <= 1 else None

    monkeypatch.setattr(agent_runner_mod, "_read_proc_start_time", _flaky)
    try:
        assert AgentRunner().restore_orphan_tasks() == [task.id]
        assert getattr(task, "subprocess_pid", None) == pgid, (
            "pid discarded on an unverifiable verdict"
        )
        assert getattr(task, "subprocess_start_time", None) is not None
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
        app_state.agents.pop(agent.id, None)


def test_gateway_declines_when_leader_identity_is_unverifiable(monkeypatch):
    """An unreadable start time is not absence, so the gateway must decline.

    _group_is_still is now the single door every killpg goes through, so a None
    start time treated as "leader merely exited" would re-introduce the recycled-
    pgid kill at kill_task and shutdown too, not just in orphan recovery.
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    try:
        # Baseline recorded, but the leader's stat is unreadable right now while
        # the pid is very much present: unverifiable, so decline.
        monkeypatch.setattr(agent_runner_mod, "_read_proc_start_time", lambda _p: None)
        assert not agent_runner_mod._group_is_still(pgid, 12345)
        assert not agent_runner_mod._killpg_verified(pgid, 12345, signal.SIGKILL)
        # No baseline at all is equally unverifiable while the pid is held.
        assert not agent_runner_mod._group_is_still(pgid, None)
        time.sleep(0.3)
        assert _alive(worker), "gateway signaled a group it could not verify"
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)


def test_successful_task_with_stale_pid_is_not_marked_failed(monkeypatch, tmp_path):
    """_finish_task persists success before the pid is cleared and persisted.

    A crash in that window leaves a genuinely successful task holding a stale
    pid; the retry path must not adopt it and rewrite the result to failed.
    """
    agent = Agent(name="successstale", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="successstale")
    task.status = TaskStatus.success
    object.__setattr__(task, "subprocess_pid", 999999)  # stale, long gone
    object.__setattr__(task, "subprocess_start_time", 12345)
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    try:
        assert AgentRunner().restore_orphan_tasks() == []
        assert task.status == TaskStatus.success, (
            "a completed task was rewritten to failed over a stale pid"
        )
    finally:
        app_state.agents.pop(agent.id, None)


def test_kill_task_does_not_signal_a_recycled_pgid(monkeypatch, tmp_path):
    """kill_task no longer gates on returncode, so it must gate on identity.

    Dropping the returncode guard is what makes this reachable: the direct child
    can already be reaped while _run_pipe_mode finalizes, freeing the pid for
    reuse before kill_task runs.
    """

    async def _run():
        runner, task, proc, reader = await _spawn(monkeypatch, tmp_path)
        impostor = [p for p in _pgroup_members(proc.pid) if p != proc.pid]
        assert impostor, "fixture spawned no second group member"
        # Report a different start time for the leader: the group stays alive, so
        # only an identity check can tell this from "our group, leader exited".
        real = agent_runner_mod._read_proc_start_time
        monkeypatch.setattr(
            agent_runner_mod,
            "_read_proc_start_time",
            lambda p: (real(p) or 0) + 1 if p == proc.pid else real(p),
        )
        try:
            await runner.kill_task(task.id)
            await asyncio.sleep(0.5)
            assert _alive(impostor[0]), "kill_task signaled a recycled pgid"
        finally:
            for p in [proc.pid] + impostor:
                try:
                    os.kill(p, 9)
                except ProcessLookupError:
                    pass
            reader.cancel()

    asyncio.run(_run())


def test_leaderless_branch_retains_pid_when_members_remain(monkeypatch, tmp_path):
    """The leaderless branch re-enumerates instead of assuming its kills took.

    A member wedged uninterruptibly, or one forked after enumeration, survives
    _kill_verified — and this branch used to clear the pgid regardless, which is
    the only handle on it.
    """
    pgid, survivor = _leaderless_group()
    agent = Agent(name="leaderless-retain", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="leaderless-retain")
    task.status = TaskStatus.running
    object.__setattr__(task, "subprocess_pid", pgid)
    object.__setattr__(task, "subprocess_start_time", 12345)  # stale on purpose
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    # Stand in for an uninterruptible member: the kills are swallowed, so the
    # group is still populated when the metadata would be cleared.
    monkeypatch.setattr(agent_runner_mod, "_kill_verified", lambda *a, **k: None)
    try:
        assert AgentRunner().restore_orphan_tasks() == [task.id]
        assert getattr(task, "subprocess_pid", None) == pgid, (
            "pid discarded while group members remained"
        )
    finally:
        try:
            os.kill(survivor, 9)
        except ProcessLookupError:
            pass
        app_state.agents.pop(agent.id, None)


def test_failed_task_with_retained_pid_is_retried(monkeypatch, tmp_path):
    """Retaining the pid is only useful if a later startup actually revisits it.

    restore_orphan_tasks scans running/waiting, but the retaining branch marks
    the task failed on its way out — so the promised retry needs the scan to also
    pick up failed tasks that still carry pid metadata.
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    agent = Agent(name="retry", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="retry")
    task.status = TaskStatus.failed  # as the retaining branch left it
    object.__setattr__(task, "subprocess_pid", pgid)
    object.__setattr__(task, "subprocess_start_time", _read_proc_start_time(pgid))
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    worker_id = _identity(worker)
    try:
        assert AgentRunner().restore_orphan_tasks() == [task.id], (
            "failed task with a retained pid was skipped; the retry never happens"
        )
        for _ in range(30):
            if not _alive_as(worker, worker_id):
                break
            time.sleep(0.1)
        assert not _alive_as(worker, worker_id), "retry did not kill the group"
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
        app_state.agents.pop(agent.id, None)


def test_reallocated_pid_between_checks_is_not_signaled(monkeypatch, tmp_path):
    """ENOENT is a point-in-time fact, so it is rechecked after enumeration.

    A pid absent at the first check can be handed out before the group is
    enumerated; the new holder calling setsid creates a group with that very
    pgid, which enumeration would return as a "survivor".
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    worker_id, leader_id = _identity(worker), _identity(pgid)
    agent = Agent(name="realloc", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="realloc")
    task.status = TaskStatus.running
    object.__setattr__(task, "subprocess_pid", pgid)
    object.__setattr__(task, "subprocess_start_time", 999999)  # mismatch
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    # The group is real and live throughout. Only the FIRST absence check lies,
    # standing in for "absent when observed, re-allocated a moment later".
    calls = {"n": 0}

    def _absent_once(_pid):
        calls["n"] += 1
        return calls["n"] == 1

    monkeypatch.setattr(agent_runner_mod, "_pid_is_absent", _absent_once)
    try:
        assert AgentRunner().restore_orphan_tasks() == [task.id]
        assert calls["n"] >= 2, "absence was never rechecked after enumeration"
        time.sleep(0.5)  # let a wrongly-issued kill land before concluding
        assert _alive_as(worker, worker_id), "re-allocated pgid's group was killed"
        assert _alive_as(pgid, leader_id), "re-allocated pid was killed"
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
        app_state.agents.pop(agent.id, None)


def test_orphan_recovery_retains_pid_when_group_survives_sigkill(
    monkeypatch, tmp_path
):
    """Clearing the pid while the group lives trades a recoverable orphan for a
    permanent one — SIGKILL can stay pending on an uninterruptible member."""
    pgid, worker, leader = _live_group_with_stubborn_worker()
    agent = Agent(name="pendingkill", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="pendingkill")
    task.status = TaskStatus.running
    object.__setattr__(task, "subprocess_pid", pgid)
    object.__setattr__(task, "subprocess_start_time", _read_proc_start_time(pgid))
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    # Stand in for a member wedged uninterruptibly: signals are swallowed, so
    # the group is still alive when the metadata would be cleared.
    monkeypatch.setattr(agent_runner_mod.os, "killpg", lambda *a, **k: None)
    try:
        assert AgentRunner().restore_orphan_tasks() == [task.id]
        assert getattr(task, "subprocess_pid", None) == pgid, (
            "pid discarded while its group was still alive"
        )
        assert getattr(task, "subprocess_start_time", None) is not None
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
        app_state.agents.pop(agent.id, None)


async def _check_shutdown_drops_a_recycled_pgid(monkeypatch, tmp_path):
    """A pgid retained across the drain must not absorb a SIGKILL after reuse.

    signaled_pgids survives up to 10s of draining. If the group exits early and
    the kernel re-leases the number, escalating on the bare number sends SIGKILL
    to whatever holds it now.

    Two things must hold, and only the second distinguishes the fix from
    `_pgroup_alive` alone: the impostor group survives, AND the SIGKILL phase
    actually ran (otherwise the survival is vacuous). A second, genuinely-ours
    lingering group forces that phase to execute.
    """
    runner, task, proc, reader = await _spawn(
        monkeypatch, tmp_path, wrapper=_eof_then_stubborn_wrapper
    )
    impostor = [p for p in _pgroup_members(proc.pid) if p != proc.pid]
    assert impostor, "fixture spawned no second group member"
    reader.add_done_callback(lambda _f: runner._live_runs.pop("run-killtest", None))
    runner._live_runs["run-killtest"]["task"] = reader

    # A second group that IS still ours: it keeps _live_runs/lingering non-empty
    # so shutdown() reaches the SIGKILL phase, where the recycled pgid would be
    # killed if it were still tracked by bare number.
    victim_pgid, victim_worker, victim_leader = _live_group_with_stubborn_worker()
    runner._live_runs["run-victim"] = {"pid": victim_pgid}

    # Report a different start time for the first group's leader from now on.
    # Everything else about /proc stays truthful, so its group still reads alive
    # — which is precisely what _pgroup_alive cannot distinguish from reuse.
    real = agent_runner_mod._read_proc_start_time
    monkeypatch.setattr(
        agent_runner_mod,
        "_read_proc_start_time",
        lambda p: (real(p) or 0) + 1 if p == proc.pid else real(p),
    )
    try:
        await runner.shutdown()
        await asyncio.sleep(0.5)
        assert not _alive(victim_worker), (
            "SIGKILL phase never ran; the impostor's survival proves nothing"
        )
        assert _alive(impostor[0]), "SIGKILL sent to a recycled pgid's group"
    finally:
        for p in [proc.pid, victim_pgid, victim_worker] + impostor:
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        victim_leader.wait(timeout=10)


def test_shutdown_drops_a_recycled_pgid_instead_of_killing_it(monkeypatch, tmp_path):
    asyncio.run(_check_shutdown_drops_a_recycled_pgid(monkeypatch, tmp_path))


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


async def _check_cleanup_retains_pid_when_group_unverifiable(monkeypatch, tmp_path):
    """kill_task 全程 UNKNOWN 时，per-run cleanup 不能清掉 pid 元数据。

    闸门对 _GROUP_UNKNOWN 两次投递都拒发，可 handle 版 SIGTERM 仍会让直接子进程退出，
    抗 SIGTERM 的后代活下来。此时 subprocess_pid 是它唯一的把手，而
    _cleanup_run_resources 原来无条件清空 —— 可恢复的孤儿就变成永久的。
    """
    runner, task, proc, reader = await _spawn(
        monkeypatch, tmp_path, wrapper=_stubborn_wrapper
    )
    stubborn = [p for p in _descendants(proc.pid) if _alive(p)]
    assert stubborn, "fixture spawned no SIGTERM-ignoring child"
    # 这个 run 才是资源的主人，否则 cleanup 会提前 return，测不到清理分支。
    runner._run_ids[task.id] = "run-killtest"
    # /proc 从此读不出身份：_group_state 一路 UNKNOWN，闸门拒发两次。
    # 两个探针都要打：只打 _read_proc_start_time 的话，leader 一退出
    # _pid_is_absent 就返回 True，"leader 只是退了"那条路又把状态推回 OURS/GONE，
    # 复现不出"始终无法核验"。EACCES/EIO 这类读失败才是 codex 描述的场景 ——
    # 目录还在，但读不出来，既不是缺席也不是身份不符。
    monkeypatch.setattr(agent_runner_mod, "_read_proc_start_time", lambda _p: None)
    monkeypatch.setattr(agent_runner_mod, "_pid_is_absent", lambda _p: False)
    try:
        await runner.kill_task(task.id)
        assert task.id in runner._retain_pid, (
            "unknown verdict did not mark the pid for retention"
        )
        runner._cleanup_run_resources(task.id, "run-killtest")
        assert getattr(task, "subprocess_pid", None) == proc.pid, (
            "pid discarded while the group's fate was unknown"
        )
        assert getattr(task, "subprocess_start_time", None) is not None
    finally:
        for p in [proc.pid] + stubborn:
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        reader.cancel()


def test_cleanup_retains_pid_when_group_verification_is_unknown(monkeypatch, tmp_path):
    asyncio.run(_check_cleanup_retains_pid_when_group_unverifiable(monkeypatch, tmp_path))


async def _check_cleanup_clears_pid_when_group_is_gone(monkeypatch, tmp_path):
    """反向守卫：组确实死了就必须清掉 pid，别把保留变成永远不清。

    留着一个已释放的 pid 会让下次启动把它当孤儿再查一遍，也可能撞上被回收的号。
    """
    runner, task, proc, reader = await _spawn(monkeypatch, tmp_path)
    tree = [proc.pid] + _descendants(proc.pid)
    runner._run_ids[task.id] = "run-killtest"
    try:
        await runner.kill_task(task.id)
        runner._cleanup_run_resources(task.id, "run-killtest")
        assert getattr(task, "subprocess_pid", None) is None, (
            "pid retained even though the group is provably gone"
        )
    finally:
        for p in tree:
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        reader.cancel()


def test_cleanup_clears_pid_when_group_is_provably_gone(monkeypatch, tmp_path):
    asyncio.run(_check_cleanup_clears_pid_when_group_is_gone(monkeypatch, tmp_path))


async def _check_retained_pgid_survives_resume(monkeypatch, tmp_path):
    """resume 覆盖 subprocess_pid 后，旧组仍要有把手。

    kill_task 因组仍为 UNKNOWN/OURS 保留了旧 pid，但 send_input 默认立刻起新进程，
    spawn 会把 subprocess_pid 改写成新 pid —— 若那是唯一的把手，也许还握着 writer
    lock 的旧组就永远回收不了（新 run 若同样失败，它的 cleanup 清掉的是新 pid）。
    retained_pgids 与 subprocess_pid 分开存，所以覆盖之后仍在，且启动时能被收割。
    """
    runner, task, proc, reader = await _spawn(
        monkeypatch, tmp_path, wrapper=_stubborn_wrapper
    )
    old_pgid = proc.pid
    old_start = _read_proc_start_time(old_pgid)
    stubborn = [p for p in _descendants(old_pgid) if _alive(p)]
    assert stubborn, "fixture spawned no SIGTERM-ignoring child"
    stubborn_ids = [(p, _read_proc_start_time(p)) for p in stubborn]
    runner._run_ids[task.id] = "run-killtest"
    # /proc 读不出身份：_group_state 一路 UNKNOWN，闸门两次投递都拒发。两个探针都要
    # 打，理由见 _check_cleanup_retains_pid_when_group_unverifiable。
    monkeypatch.setattr(agent_runner_mod, "_read_proc_start_time", lambda _p: None)
    monkeypatch.setattr(agent_runner_mod, "_pid_is_absent", lambda _p: False)
    try:
        await runner.kill_task(task.id)
        assert [old_pgid] == [
            p for p, _s in (getattr(task, "retained_pgids", None) or [])
        ], "unverifiable group was not recorded outside subprocess_pid"
        # resume 起的新进程会覆盖 subprocess_pid（两条 spawn 路径都这么做），并清掉
        # _retain_pid —— 元数据从此描述的是新进程。
        object.__setattr__(task, "subprocess_pid", 999999)
        object.__setattr__(task, "subprocess_start_time", 1)
        runner._retain_pid.discard(task.id)
        assert [old_pgid] == [
            p for p, _s in (getattr(task, "retained_pgids", None) or [])
        ], "retained handle was lost when resume overwrote subprocess_pid"
    finally:
        # 恢复真实探针，否则下面的收割和断言都读不出身份。
        monkeypatch.setattr(
            agent_runner_mod, "_read_proc_start_time", _read_proc_start_time
        )
        monkeypatch.setattr(agent_runner_mod, "_pid_is_absent", _real_pid_is_absent)
    try:
        # 把手确实能用：restore_orphan_tasks 为 retained_pgids 单独扫一趟。记录的
        # 身份必须是真的，否则闸门（正确地）拒发。
        object.__setattr__(task, "retained_pgids", [[old_pgid, old_start]])
        object.__setattr__(task, "subprocess_pid", None)
        object.__setattr__(task, "subprocess_start_time", None)
        task.status = TaskStatus.success  # 与任务当前状态无关，扫描不该漏掉它
        monkeypatch.setattr(app_state, "tasks", {task.id: task})
        runner.restore_orphan_tasks()
        for pid_, ident in stubborn_ids:
            assert not _alive_as(pid_, ident), (
                "retained group survived the startup sweep"
            )
        assert not (getattr(task, "retained_pgids", None) or []), (
            "handle kept even though the group is provably gone"
        )
    finally:
        for p in [old_pgid] + stubborn:
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        reader.cancel()


def test_retained_pgid_survives_resume_overwriting_the_pid(monkeypatch, tmp_path):
    asyncio.run(_check_retained_pgid_survives_resume(monkeypatch, tmp_path))


async def _check_shutdown_drains_retained_pgids(monkeypatch, tmp_path):
    """`run.sh stop` 也要收割 retained_pgids，不能只等下次启动。

    kill_task 保留了一个扛过 SIGTERM 的旧组，随后的 resume 把 subprocess_pid 换成了
    新 pid —— 旧组从此不在 _live_runs 里，shutdown 的信号集合也就看不到它。只靠
    restore_orphan_tasks 收割意味着 `bash run.sh stop`（后面不接 start）会在旧组仍
    握着 codex writer lock 时退出后端，那个组就永远留在机器上了。
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    worker_id = _identity(worker)
    agent = Agent(name="retaindrain", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="retaindrain")
    # 任务当前状态与把手无关：resume 之后它可能已经跑成功了，旧组照样还在。
    task.status = TaskStatus.success
    object.__setattr__(task, "retained_pgids", [[pgid, _read_proc_start_time(pgid)]])
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    runner = AgentRunner()
    # _live_runs 空：这正是缺陷成立的前提，旧组没有任何 live run 可以搭便车。
    assert not runner._live_runs
    try:
        await runner.shutdown()
        for _ in range(30):
            if not _alive_as(worker, worker_id):
                break
            await asyncio.sleep(0.1)
        assert not _alive_as(worker, worker_id), (
            "retained group outlived shutdown; run.sh stop would leak it"
        )
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
        app_state.agents.pop(agent.id, None)


def test_shutdown_drains_retained_pgids(monkeypatch, tmp_path):
    asyncio.run(_check_shutdown_drains_retained_pgids(monkeypatch, tmp_path))


async def _check_shutdown_signals_every_identity_of_a_reused_pgid(
    monkeypatch, tmp_path
):
    """同一个 pgid 号在元数据里出现多次时，每个身份都要各自过闸门。

    retained_pgids 是 append-only 的：一个旧组还没被证明消失，它的号就可能被后来的
    run 复用，于是两条 [pgid, start] 记录并存到下次启动复核为止。若 shutdown 按
    pgid 去重（只留先记下的身份），那条陈旧身份每轮都被判 FOREIGN 剔掉、下一轮又从
    元数据里加回来，真正对得上活组的那个身份则永远进不了集合 —— `run.sh stop` 从此
    对这个仍握着 codex writer lock 的组一个信号都不发。
    """
    pgid, worker, leader = _live_group_with_stubborn_worker(stubborn=False)
    worker_id = _identity(worker)
    real_start = _read_proc_start_time(pgid)
    agent = Agent(name="reusedpgid", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="reusedpgid")
    task.status = TaskStatus.success
    # 先记下的是那条陈旧身份（号已被复用，start 对不上），真正的活组排在后面。
    object.__setattr__(
        task, "retained_pgids", [[pgid, (real_start or 0) - 12345], [pgid, real_start]]
    )
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    runner = AgentRunner()
    assert not runner._live_runs
    try:
        await runner.shutdown()
        assert not _alive_as(worker, worker_id), (
            "活组的身份被同号的陈旧记录挤掉了，shutdown 没给它发过任何信号"
        )
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
        app_state.agents.pop(agent.id, None)


def test_shutdown_signals_every_identity_of_a_reused_pgid(monkeypatch, tmp_path):
    asyncio.run(
        _check_shutdown_signals_every_identity_of_a_reused_pgid(monkeypatch, tmp_path)
    )


async def _check_shutdown_waits_for_retained_only_groups(monkeypatch, tmp_path):
    """只剩保留组时，两个 deadline 轮询都必须把它算进去。

    _live_runs 为空时，SIGTERM 的等待循环整个被跳过、SIGKILL 立刻发出、之后 5s 的
    复检循环同样被跳过 —— 保留组既没拿到 graceful TERM 窗口，投递之后也没人复核。
    一个正在干净退出（或 SIGKILL 尚挂在不可中断等待里）的成员就这样活过了
    `run.sh stop`，writer lock 泄漏依旧。
    """
    marker = tmp_path / "worker-exited-cleanly"
    # leader 用默认 SIGTERM 行为：它先退，组只剩 worker，worker 干净退出后组即 GONE，
    # 循环立刻收敛，不必耗掉整个 10s 预算。
    pgid, worker, leader = _live_group_with_graceful_worker(marker)
    worker_id = _identity(worker)
    agent = Agent(name="retainwait", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="retainwait")
    task.status = TaskStatus.success
    object.__setattr__(task, "retained_pgids", [[pgid, _read_proc_start_time(pgid)]])
    app_state.agents[agent.id] = agent
    monkeypatch.setattr(app_state, "tasks", {task.id: task})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    runner = AgentRunner()
    assert not runner._live_runs
    try:
        await runner.shutdown()
        assert marker.exists(), (
            "保留组在 SIGTERM 之后没等就被 SIGKILL 了，等于没有 graceful 窗口"
        )
        # shutdown 返回时必须已经复核过：不能投完信号就退出。
        assert not _alive_as(worker, worker_id), (
            "shutdown 返回时保留组还活着，说明投递之后没有任何等待/复检"
        )
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
        app_state.agents.pop(agent.id, None)


def test_shutdown_waits_for_retained_only_groups(monkeypatch, tmp_path):
    asyncio.run(_check_shutdown_waits_for_retained_only_groups(monkeypatch, tmp_path))


async def _check_shutdown_rechecks_retained_after_sigkill(monkeypatch, tmp_path):
    """SIGKILL 之后也要复检保留组，不能投完一发就返回。

    SIGKILL 不是同步的：卡在不可中断等待里的成员会让它挂起，组暂时仍是 OURS/UNKNOWN。
    escalation 阶段的复检循环若只看 _live_runs，只剩保留组时它一轮都不跑 —— 一次投递
    之后 shutdown 直接返回，那个组就活过了 `run.sh stop`。

    这里把闸门与判决都换成探针：`retained_pgids` 在 SIGTERM 阶段之后才出现（drain 期间
    落地的 kill_task 就是这个形状），所以 SIGTERM 循环整个被跳过，SIGKILL 阶段成为唯一
    的等待机会；判决连续几轮报 UNKNOWN，模拟 SIGKILL 尚未落地。
    """
    agent = Agent(name="retainrecheck", command="/bin/true", cwd=str(tmp_path))
    task = Task(agent_id=agent.id, name="retainrecheck")
    task.status = TaskStatus.success
    pgid, leader_start = 424242, 999
    object.__setattr__(task, "retained_pgids", [[pgid, leader_start]])
    app_state.agents[agent.id] = agent

    class _LateTasks:
        """第一次读为空，之后才含这个任务 —— 把手在 SIGTERM 阶段之后才登记。"""

        def __init__(self):
            self.reads = 0

        def values(self):
            self.reads += 1
            return [] if self.reads == 1 else [task]

    monkeypatch.setattr(app_state, "tasks", _LateTasks())
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    verdicts = {"n": 0}

    def fake_state(_pgid, _start):
        verdicts["n"] += 1
        # 前几轮 SIGKILL 还没落地；之后组才可证明消失，循环随即收敛。
        return (
            agent_runner_mod._GROUP_UNKNOWN if verdicts["n"] <= 4
            else agent_runner_mod._GROUP_GONE
        )

    attempts = []
    monkeypatch.setattr(agent_runner_mod, "_group_state", fake_state)
    monkeypatch.setattr(
        agent_runner_mod,
        "_killpg_verified",
        lambda p, s, sig: attempts.append((p, sig)) or False,
    )
    try:
        await AgentRunner().shutdown()
        kills = [a for a in attempts if a == (pgid, signal.SIGKILL)]
        assert len(kills) >= 2, (
            "SIGKILL 只投了一发就返回，保留组在 escalation 阶段没有被复检"
        )
        assert verdicts["n"] > 4, "shutdown 没有等到保留组出现确定判决"
    finally:
        app_state.agents.pop(agent.id, None)


def test_shutdown_rechecks_retained_after_sigkill(monkeypatch, tmp_path):
    asyncio.run(_check_shutdown_rechecks_retained_after_sigkill(monkeypatch, tmp_path))


async def _check_shutdown_rechecks_signaled_after_sigkill(monkeypatch, tmp_path):
    """独立跟踪的组（signaled_pgids）在 SIGKILL 之后同样要复检。

    wrapper 收到 SIGTERM 后退出、_on_done 随即把 run 移出 _live_runs，而一个后代还活着：
    此时那个组既不在 _live_runs 里，也不在 retained_pgids 里（后者只装 kill_task 记下的
    旧组），唯一的把手就是 signaled_pgids。若 SIGKILL 阶段的复检循环只看
    _live_runs/retained_pgids，投完一发 SIGKILL 就返回 —— 暂时卡在不可中断等待里的成员
    就活过了 `run.sh stop`，codex writer lock 照旧泄漏。

    这里用一个只有 pid、没有 task/proc 的 live run（wrapper 已退出、entry 尚在的形状）
    喂进 signaled_pgids，然后让判决连续几轮报 UNKNOWN 模拟 SIGKILL 尚未落地。

    SIGTERM 的 graceful 窗口压到 0：那一段现在同样会为 signaled_pgids 轮询（这是本轮另一
    条修复），不压小的话组会在 graceful 阶段就等到确定判决，SIGKILL 阶段反而无事可做，
    这个断言就测不到它本来要守的东西了。
    """
    pgid, leader_start = 515151, 777
    monkeypatch.setattr(app_state, "tasks", {})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    monkeypatch.setattr(agent_runner_mod, "_sigterm_grace_seconds", lambda: 0)
    verdicts = {"n": 0}

    def fake_state(_pgid, _start):
        verdicts["n"] += 1
        # 前几轮 SIGKILL 还没落地：组仍是 UNKNOWN，不能当成消失。
        return (
            agent_runner_mod._GROUP_UNKNOWN if verdicts["n"] <= 4
            else agent_runner_mod._GROUP_GONE
        )

    attempts = []
    monkeypatch.setattr(agent_runner_mod, "_group_state", fake_state)
    monkeypatch.setattr(
        agent_runner_mod,
        "_killpg_verified",
        lambda p, s, sig: attempts.append((p, sig)) or False,
    )
    runner = AgentRunner()
    # 只有 pid：没有 task 可 await，也没有 proc/transport —— 正是 _on_done 把 entry
    # 摘掉前后那一瞬的形状。SIGTERM 首发把它登记进 signaled_pgids。
    runner._live_runs["run-signaled"] = {"pid": pgid, "pid_start": leader_start}

    async def _drop_entry():
        # SIGTERM 之后 wrapper 退出，_on_done 移除 entry；组只剩 signaled_pgids 这一份
        # 把手。用一个独立协程做，避免依赖 shutdown 内部的调用次序。
        await asyncio.sleep(0.05)
        runner._live_runs.pop("run-signaled", None)

    dropper = asyncio.ensure_future(_drop_entry())
    try:
        await runner.shutdown()
        kills = [a for a in attempts if a == (pgid, signal.SIGKILL)]
        assert len(kills) >= 2, (
            "SIGKILL 只投了一发就返回，独立跟踪的组在 escalation 阶段没有被复检"
        )
        assert verdicts["n"] > 4, "shutdown 没有等到该组出现确定判决"
    finally:
        dropper.cancel()
        runner._live_runs.clear()


def test_shutdown_rechecks_signaled_groups_after_sigkill(monkeypatch, tmp_path):
    asyncio.run(_check_shutdown_rechecks_signaled_after_sigkill(monkeypatch, tmp_path))


async def _check_sigterm_grace_covers_signaled_only_groups(monkeypatch, tmp_path):
    """SIGTERM 的 graceful 窗口也要把 signaled_pgids 算进去。

    wrapper 收到 SIGTERM 立刻退出、后代关掉 stdout 但仍在 graceful cleanup：_on_done
    把唯一那条 _live_runs 记录摘掉，组就只剩 signaled_pgids 这一份把手（不属于任何 live
    run，也不在 retained_pgids 里 —— 那份只装 kill_task 记下的旧组）。循环条件若只看
    _live_runs/retained_pgids，此刻立即为假，shutdown 直接跳到 SIGKILL —— 承诺的 10s
    graceful 窗口那个后代一秒都没拿到，收尾写一半就被打断。

    worker 装好 handler 后要 3s 才干净退出并留下 marker；marker 存在即证明它拿到了
    graceful 窗口，不存在就说明 SIGKILL 在同一个事件循环回合里就落下来了。

    delay 特意远大于 0.6s：_group_state 每次都要扫一遍 /proc（约 0.3s），坏实现在
    "break → prune → escalate" 这一路上会白捡几百毫秒的间隔，短 delay 会被这点偶然
    余量盖住，测试就成了空守卫。
    """
    marker = tmp_path / "worker-exited-cleanly"
    pgid, worker, leader = _live_group_with_graceful_worker(marker, delay=3.0)
    worker_id = _identity(worker)
    monkeypatch.setattr(app_state, "tasks", {})
    monkeypatch.setattr(app_state, "save_agent_tasks", lambda *a, **k: None)
    runner = AgentRunner()
    # 只有 pid：没有 task 可 await，也没有 proc/transport —— _on_done 摘掉 entry 前后
    # 那一瞬的形状。SIGTERM 首发把它登记进 signaled_pgids。
    runner._live_runs["run-signaled"] = {
        "pid": pgid, "pid_start": _read_proc_start_time(pgid),
    }

    async def _drop_entry():
        # wrapper 退出、_on_done 移除 entry：组只剩 signaled_pgids 这一份把手。
        await asyncio.sleep(0.05)
        runner._live_runs.pop("run-signaled", None)

    dropper = asyncio.ensure_future(_drop_entry())
    try:
        await runner.shutdown()
        assert marker.exists(), (
            "_live_runs 一空 SIGTERM 窗口就结束了，独立跟踪的组没拿到 graceful 间隔"
        )
        assert not _alive_as(worker, worker_id), (
            "shutdown 返回时该组还活着，说明投递之后没有任何等待/复检"
        )
    finally:
        dropper.cancel()
        runner._live_runs.clear()
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)


def test_sigterm_grace_covers_signaled_only_groups(monkeypatch, tmp_path):
    asyncio.run(_check_sigterm_grace_covers_signaled_only_groups(monkeypatch, tmp_path))


async def _check_unfinished_notifications_are_cancelled(monkeypatch):
    """通知 drain 超时后必须显式取消，不能留给事件循环 teardown 静默丢弃。

    进程组两阶段吃满 17s、consolidation 还占着 13s 时，通知 drain 只剩约 60s，装不下
    80s 的最坏情况 —— 也就是说"外层等待先超时"是常态而非边角。asyncio.wait 的 timeout
    并不取消 pending 任务，于是 send_feishu_card 的 except CancelledError（杀掉并回收
    feishu-bot CLI 子进程的地方）永远不跑：run.sh 只 signal 后端 PID，那个 CLI 就在后端
    退出后成了孤儿。丢掉最新那张卡是可接受的降级，漏下子进程不是。
    """
    budget = 6
    monkeypatch.setattr(
        agent_runner_mod, "_shutdown_total_budget_seconds", lambda: budget
    )
    runner = AgentRunner()
    assert not runner._live_runs
    reaped = {"done": False}

    async def _hung_notify():
        try:
            await asyncio.sleep(600)
        except asyncio.CancelledError:
            # send_feishu_card 的取消分支就长这样：kill 之后 await 一个 shielded 的 reap
            # 再抛出。收尾需要真正的 await 回合，所以取消之后必须还剩时间给它跑完 ——
            # 那正是 reserve 存在的理由。shield 是因为这本就在取消路径上。
            await asyncio.shield(asyncio.sleep(0.3))
            reaped["done"] = True
            raise

    notify = asyncio.ensure_future(_hung_notify())
    runner._notify_tasks.add(notify)
    try:
        await runner.shutdown()
        assert notify.cancelled() or notify.done(), (
            "未完成的通知没被显式取消，feishu-bot CLI 子进程会成为孤儿"
        )
        assert reaped["done"], (
            "通知的取消分支没跑完 —— 取消之后没有留出 kill-and-reap 的时间"
        )
    finally:
        notify.cancel()
        await asyncio.gather(notify, return_exceptions=True)


def test_unfinished_notifications_are_cancelled_and_reaped(monkeypatch):
    asyncio.run(_check_unfinished_notifications_are_cancelled(monkeypatch))


async def _check_shutdown_stays_within_its_total_budget(monkeypatch):
    """所有等待共享一个总预算，不能各阶段独立计时再相加。

    原来 SIGTERM(10) + SIGKILL(5) + cancel(2) + 通知 drain(80) + consolidation
    drain/cancel(5+8) 是逐段累加的，最坏 110s > run.sh 给 backend 的 95s grace ——
    超时后端被 kill -9，通知卡的收尾与 helper 进程的 kill-and-reap 都不会跑（丢卡 +
    孤儿 glm/cco）。这里把总预算压到 20s，喂进永不结束的通知与 consolidation 任务：
    shutdown 必须在预算内返回，而不是把 80+5+8 各等满。
    """
    budget = 20
    monkeypatch.setattr(
        agent_runner_mod, "_shutdown_total_budget_seconds", lambda: budget
    )
    runner = AgentRunner()
    assert not runner._live_runs
    hung = [
        asyncio.ensure_future(asyncio.sleep(600)),
        asyncio.ensure_future(asyncio.sleep(600)),
    ]
    runner._notify_tasks.add(hung[0])
    runner._consolidate_tasks.add(hung[1])
    loop = asyncio.get_event_loop()
    started = loop.time()
    try:
        await runner.shutdown()
        elapsed = loop.time() - started
        assert elapsed <= budget + 3, (
            f"shutdown 用了 {elapsed:.1f}s，超出总预算 {budget}s —— 各阶段仍在独立累加，"
            "run.sh 的 95s grace 会被越过"
        )
        # 反向：也不能因为收敛预算就把每个窗口压成 0，drain 本来的意义就没了。
        assert elapsed >= 3, (
            f"shutdown 只用了 {elapsed:.1f}s，通知/consolidation 根本没拿到 drain 窗口"
        )
        # consolidation 超时后必须被显式 cancel（其 except CancelledError 才是 kill
        # helper 子进程的地方），不能留给事件循环 teardown 静默丢弃。
        assert hung[1].cancelled() or hung[1].done(), (
            "未完成的 consolidation 没被显式取消，helper 会成为孤儿"
        )
    finally:
        for t in hung:
            t.cancel()
        await asyncio.gather(*hung, return_exceptions=True)


def test_shutdown_stays_within_its_total_budget(monkeypatch):
    asyncio.run(_check_shutdown_stays_within_its_total_budget(monkeypatch))


def test_shutdown_budget_fits_run_sh_backend_grace():
    """总预算必须真的小于 run.sh 里 backend 的 stop grace。

    两个数字分居两个文件，改了一边忘了另一边就等于没有这道闸门：预算若涨过 grace，
    `bash run.sh stop` 依旧会在 shutdown 收尾前 kill -9 后端。
    """
    import re

    run_sh = (Path(__file__).resolve().parent.parent / "run.sh").read_text()
    m = re.search(r'stop_one\s+"backend"\s+"\$BACKEND_PID"\s+(\d+)', run_sh)
    assert m, "run.sh 里找不到 backend 的 stop grace，闸门失效"
    grace = int(m.group(1))
    budget = agent_runner_mod._shutdown_total_budget_seconds()
    assert budget < grace, (
        f"shutdown 总预算 {budget}s 不小于 run.sh 的 backend grace {grace}s"
    )


def test_start_time_survives_a_comm_with_spaces(tmp_path, monkeypatch):
    """进程名含空格时，身份必须还是 stat 的第 22 字段。

    整行 split() 会被括号里的 comm 撑开：`(foo bar)` 让后面每个字段右移一位，读到的
    "start time" 其实是别的字段（常见结果是 0）。这样算出来的假身份还会撞车 —— 另一
    个同样带空格名字的进程复用了 pid/pgid 后得到同一个值，_group_state 就把陌生人的
    组认成我们的，闸门放行 killpg。
    """
    # 真实 stat 行的形状，comm 里塞进空格和括号（内核只是原样填进程名）。
    # 前两字段之后的第 20 个（state 起算）才是 starttime = 987654。
    tail = " ".join(["S", "1", "4242"] + ["0"] * 16 + ["987654"] + ["0"] * 30)
    fake_stat = f"4242 (evil ) name) {tail}\n"

    fields = agent_runner_mod._stat_fields_after_comm(fake_stat)
    assert fields[0] == "S"
    assert fields[2] == "4242"  # pgrp，_scan_pgroup 的匹配依据
    assert fields[19] == "987654"

    # _read_proc_start_time 走同一条解析路径：打掉读文件，只验字段切分。
    monkeypatch.setattr(
        agent_runner_mod.Path, "read_text", lambda self, **kw: fake_stat
    )
    assert agent_runner_mod._read_proc_start_time(4242) == 987654

    # 不是合法 stat（没有右括号）时按"读取失败"处理，而不是按错位字段下判决。
    assert agent_runner_mod._stat_fields_after_comm("no parens here") == []


def test_group_state_revalidates_a_live_leader_after_the_scan(monkeypatch):
    """活着的 leader 在扫 /proc 期间被换掉，也必须判 FOREIGN。

    _scan_pgroup 要走一遍 /proc（几百毫秒），这期间 leader 可能退出、pid 被回收并重
    新分配给一个新的 session leader，扫出来的成员就是它的。此前只有"进来时就已缺席"
    的 leader 才补检重分配，活着的 leader 走不到那个分支 —— 判成 _GROUP_OURS 后闸门
    就会向陌生人的组投信号。
    """
    pgid, worker, leader = _live_group_with_stubborn_worker()
    real = agent_runner_mod._read_proc_start_time
    real_start = real(pgid)
    calls = {"n": 0}

    def _changes_after_first_read(p):
        if p != pgid:
            return real(p)
        calls["n"] += 1
        # 第一次（扫描前的身份检查）报真身份，之后（扫描后的复检）报别人的。
        return real_start if calls["n"] == 1 else (real_start or 0) + 1

    monkeypatch.setattr(
        agent_runner_mod, "_read_proc_start_time", _changes_after_first_read
    )
    try:
        state = agent_runner_mod._group_state(pgid, real_start)
        assert state == agent_runner_mod._GROUP_FOREIGN, state
        assert not agent_runner_mod._killpg_verified(pgid, real_start, signal.SIGKILL)
        time.sleep(0.3)
        assert _alive(worker), "signaled a group whose live leader was replaced"
    finally:
        for p in (worker, pgid):
            try:
                os.kill(p, 9)
            except ProcessLookupError:
                pass
        leader.wait(timeout=10)
