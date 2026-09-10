"""Tests for the history-triggered consolidation scheduler.

Three failure modes, all reported by review and all invisible in normal
operation — nothing raises, the documents stay syntactically valid, and the only
symptom is entry frequencies drifting or a helper CLI being called in a loop:

- two completions crossing the threshold together both queue a pass, because the
  overlap guard was set inside the coroutine rather than before it was created
- a pass where every helper command failed leaves the counter untouched on
  purpose, so re-arming on it spins: subprocess and log loop for a missing CLI,
  billable retries forever for a provider returning garbage
- the window fed to consolidation must be the unconsumed one (asserted in
  test_history_layer.py, which owns the slice itself)

``_schedule_consolidate`` is exercised as an unbound method against a minimal
stand-in: AgentRunner.__init__ loads sessions and tasks off disk, and none of
that is involved in the scheduling decision.
"""
from __future__ import annotations

import asyncio

import pytest

from server import auto_memory as am, knowledge
from server.agent_runner import AgentRunner

EID = "__test_sched__"


class _Stub:
    """Just the attributes _schedule_consolidate touches."""

    def __init__(self):
        self._consolidating: set[str] = set()
        self._consolidate_tasks: set[asyncio.Task] = set()

    _schedule_consolidate = AgentRunner._schedule_consolidate


class _Msg:
    def __init__(self, content):
        self.role, self.type, self.content = "agent", "tool_result", content


class _Task:
    def __init__(self, tid):
        self.id, self.name, self.updated_at = tid, tid, "2026-09-09T10:00:00"
        self.status, self.num_turns = "failed", 1
        self.messages = [_Msg("ValueError: boom")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect memory to tmp and stub out the task lookup.

    The stub returns a real task rather than []: with no task signals and a
    consumed window, consolidate_layer short-circuits on `not signals` and makes
    no LLM call at all — which would mask a duplicate pass instead of exposing
    it.
    """
    monkeypatch.setattr(am, "MEMORY_DIR", tmp_path)
    import server.routes_ws as rw
    monkeypatch.setattr(rw, "_eid_tasks", lambda eid: [_Task("t1")])
    yield


def _reply(monkeypatch, text: str, calls: list | None = None):
    async def fake(command, prompt, timeout=0):
        if calls is not None:
            calls.append(command)
        return text

    monkeypatch.setattr(knowledge, "_llm_call", fake)


async def _drain(stub: _Stub):
    """Let every scheduled pass (and anything it re-arms) run to completion."""
    for _ in range(50):
        if not stub._consolidate_tasks:
            return
        await asyncio.wait(list(stub._consolidate_tasks), timeout=10)
        await asyncio.sleep(0)


OK = '[{"op":"add","title":"T","fact":"F"}]'
GARBAGE = "分析完成，没有新增。"


def test_two_triggers_in_the_same_tick_queue_only_one_pass(env, monkeypatch):
    """The reservation must be taken before create_task. Taken inside run(), it
    is not visible until the loop yields, so two back-to-back completions both
    saw an empty set and the second re-consolidated the same history behind the
    lock — bumping every entry's n a second time."""
    calls: list = []
    _reply(monkeypatch, OK, calls)

    async def go():
        stub = _Stub()
        for i in range(5):
            am.append_history(EID, "a", "success", f"run {i}")
        stub._schedule_consolidate(EID, 5)
        stub._schedule_consolidate(EID, 5)   # same tick, before run() starts
        await _drain(stub)
        return stub

    stub = asyncio.run(go())
    assert len(calls) == 2, f"one pass is two LLM calls (one per layer); got {calls}"
    assert stub._consolidating == set(), "the reservation must be released"


def test_a_failed_pass_does_not_re_arm_itself(env, monkeypatch):
    """consolidate() keeps the window when every command fails, so the re-arm
    condition stays true — a missing CLI would become a subprocess and log loop,
    and a provider returning malformed output would bill for retries forever
    with no new history to look at."""
    calls: list = []
    _reply(monkeypatch, GARBAGE, calls)

    async def go():
        stub = _Stub()
        for i in range(am.consolidate_every()):
            am.append_history(EID, "a", "success", f"run {i}")
        stub._schedule_consolidate(EID, am.consolidate_every())
        await _drain(stub)

    asyncio.run(go())
    # One pass = two layers × (command + retry_commands). Anything beyond that
    # means it re-armed on its own unchanged window.
    from server.config import automemory_config
    per_layer = 1 + len(automemory_config()["retry_commands"])
    assert len(calls) == 2 * per_layer, f"the failed window re-armed: {calls}"
    assert am.read_history_counter(EID) == am.consolidate_every(), \
        "a failed pass must keep its window for the next append or nightly run"


def test_a_successful_pass_re_arms_on_appends_that_arrived_mid_flight(env, monkeypatch):
    """The property the re-arm exists for: runs finishing during the two LLM
    calls bumped the counter after the snapshot, and their own trigger was
    dropped by the overlap guard."""
    every = am.consolidate_every()
    passes = []

    async def fake(command, prompt, timeout=0):
        passes.append(command)
        # First pass only: a full new window lands while it is in flight.
        if len(passes) <= 2:
            for i in range(every // 2 + 1):
                am.append_history(EID, "a", "success", f"late {len(passes)}-{i}")
        return OK

    monkeypatch.setattr(knowledge, "_llm_call", fake)

    async def go():
        stub = _Stub()
        for i in range(every):
            am.append_history(EID, "a", "success", f"run {i}")
        stub._schedule_consolidate(EID, every)
        await _drain(stub)
        return stub

    stub = asyncio.run(go())
    assert len(passes) >= 4, "a second pass must run for the mid-flight window"
    assert stub._consolidating == set()


def test_a_crashing_pass_releases_its_reservation(env, monkeypatch):
    """Otherwise the eid is wedged for the process's lifetime."""
    async def boom(command, prompt, timeout=0):
        raise RuntimeError("helper exploded")

    monkeypatch.setattr(knowledge, "_llm_call", boom)

    async def go():
        stub = _Stub()
        am.append_history(EID, "a", "success", "run")
        stub._schedule_consolidate(EID, 1)
        await _drain(stub)
        return stub

    stub = asyncio.run(go())
    assert stub._consolidating == set()


# ── which tasks a threshold pass may see ──────────────────────────────────────
#
# Reported by review after the history slice landed: slicing history.md alone was
# not enough. _eid_tasks returns every task the eid owns, both extractors walk
# their complete message lists, and the pass runs every ten completions — so old
# tool errors and corrections that fit under the signal cap were re-fed every
# cycle, letting the model update the same entries and inflate n. Same corruption
# the history slice existed to remove, arriving by the other input.

from server.agent_runner import _window_tasks


class _T:
    def __init__(self, tid, status, updated_at, messages=None):
        self.id, self.name = tid, tid
        self.status, self.updated_at = status, updated_at
        self.num_turns, self.messages = 1, messages or []


def test_only_the_newest_window_of_tasks_is_fed():
    tasks = [_T(f"t{i}", "success", f"2026-09-{i + 1:02d}") for i in range(30)]
    got = _window_tasks(tasks, 10)
    assert len(got) == 10
    assert [t.id for t in got] == [f"t{i}" for i in range(29, 19, -1)], \
        "the newest ten, newest first"


def test_in_flight_tasks_are_excluded():
    """extract_project_signals checks neither status nor streaming, so a
    half-generated agent message mentioning a path would be persisted as project
    knowledge before that run reached its conclusion."""
    tasks = [
        _T("running", "running", "2026-09-30"),
        _T("waiting", "waiting", "2026-09-29"),
        _T("idle", "idle", "2026-09-28"),
        _T("ok", "success", "2026-09-27"),
        _T("bad", "failed", "2026-09-26"),
    ]
    assert sorted(t.id for t in _window_tasks(tasks, 10)) == ["bad", "ok"]


def test_a_task_status_enum_is_matched_by_value():
    """Task.status is a TaskStatus enum in production, a str in these stubs;
    both must filter identically or the guard silently passes nothing."""
    from server.models import TaskStatus

    tasks = [_T("a", TaskStatus.success, "2026-09-02"),
             _T("b", TaskStatus.running, "2026-09-01")]
    assert [t.id for t in _window_tasks(tasks, 10)] == ["a"]


def test_a_zero_window_feeds_no_tasks():
    assert _window_tasks([_T("a", "success", "2026-09-01")], 0) == []


def test_fewer_tasks_than_the_window_is_fine():
    tasks = [_T("a", "success", "2026-09-01")]
    assert len(_window_tasks(tasks, 10)) == 1


def test_the_scheduler_passes_a_bounded_task_set(env, monkeypatch):
    """End to end: the pass must not receive the whole history of tasks."""
    seen: list[int] = []

    async def fake(command, prompt, timeout=0):
        seen.append(prompt.count("[tool_error]"))
        return OK

    monkeypatch.setattr(knowledge, "_llm_call", fake)
    import server.routes_ws as rw
    every = am.consolidate_every()
    # 40 old finished tasks, each carrying one distinct tool error.
    monkeypatch.setattr(rw, "_eid_tasks", lambda eid: [
        _T(f"t{i}", "success", f"2026-08-{i % 28 + 1:02d}",
           [_Msg(f"ValueError: boom {i}")]) for i in range(40)
    ])

    async def go():
        stub = _Stub()
        for i in range(every):
            am.append_history(EID, "a", "success", f"run {i}")
        stub._schedule_consolidate(EID, every)
        await _drain(stub)

    asyncio.run(go())
    assert seen, "the pass must have run"
    assert max(seen) <= every, \
        f"a pass fed {max(seen)} tool errors for a window of {every}"


# ── the helper subprocess must not outlive shutdown ───────────────────────────
#
# Reported by review: on `bash run.sh restart`, a glm/cco call lasting longer
# than the 5s drain was still pending when shutdown returned. Loop teardown
# cancels communicate(), which does NOT signal the child, and run.sh signals only
# the backend PID — so the helper was orphaned, left running and billing for up
# to its own 600s timeout.

def test_a_cancelled_llm_call_kills_its_helper(tmp_path):
    """Drives the real _llm_call against a real long-lived subprocess."""
    import os
    import signal

    async def go():
        # `sleep 600` stands in for a helper still thinking. The command is
        # resolved by create_subprocess_exec exactly as glm/cco would be.
        import server.knowledge as k
        started: list = []
        real_exec = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            proc = await real_exec("sleep", "600",
                                   stdout=kwargs.get("stdout"),
                                   stderr=kwargs.get("stderr"))
            started.append(proc)
            return proc

        k.asyncio.create_subprocess_exec = spy
        try:
            call = asyncio.ensure_future(k._llm_call("glm", "prompt", timeout=600))
            for _ in range(100):                 # let the child actually spawn
                await asyncio.sleep(0.01)
                if started:
                    break
            assert started, "the helper subprocess never started"
            proc = started[0]
            assert proc.returncode is None, "child should still be running"
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            return proc
        finally:
            k.asyncio.create_subprocess_exec = real_exec

    proc = asyncio.run(go())
    assert proc.returncode is not None, \
        "the helper was left running after its call was cancelled"
    # Reaped, not merely signalled: an un-awaited child becomes a zombie, and
    # run.sh's is_running treats a zombie backend PID as alive.
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


def test_a_timed_out_llm_call_also_kills_its_helper():
    """Same leak by the other exit: the timeout branch returned "" and walked
    away from the child."""
    import os

    async def go():
        import server.knowledge as k
        started: list = []
        real_exec = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            proc = await real_exec("sleep", "600",
                                   stdout=kwargs.get("stdout"),
                                   stderr=kwargs.get("stderr"))
            started.append(proc)
            return proc

        k.asyncio.create_subprocess_exec = spy
        try:
            out = await k._llm_call("glm", "prompt", timeout=1)
            assert out == "", "a timeout must read as no usable output"
            return started[0]
        finally:
            k.asyncio.create_subprocess_exec = real_exec

    proc = asyncio.run(go())
    assert proc.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


def test_a_partial_failure_does_not_spin_the_re_arm(env, monkeypatch):
    """Reported by review after window accounting went per layer: a failed layer
    keeps its pending count at the threshold forever, so a re-arm keyed on the
    maximum re-fires immediately — the succeeded layer has no signals and returns
    success without an LLM call, the failed one fails again, repeat, hammering the
    broken helper with no new history."""
    calls: list = []

    async def fake(command, prompt, timeout=0):
        calls.append(command)
        # lessons keeps succeeding, project keeps failing.
        if "错误经验提取器" in prompt:
            return OK
        return GARBAGE

    monkeypatch.setattr(knowledge, "_llm_call", fake)
    every = am.consolidate_every()

    async def go():
        stub = _Stub()
        for i in range(every):
            am.append_history(EID, "a", "success", f"run {i}")
        stub._schedule_consolidate(EID, every)
        await _drain(stub)

    asyncio.run(go())
    from server.config import automemory_config
    per_layer = 1 + len(automemory_config()["retry_commands"])
    # One pass: lessons once, project once per command.
    assert len(calls) <= 1 + per_layer, f"the partial failure re-armed: {calls}"
    # And the failed layer kept its window for a later append or nightly run.
    assert am.read_pending(EID).get("project") == every


def test_the_re_arm_still_fires_for_genuine_arrivals(env, monkeypatch):
    """The property must survive the fix: min_pending rises only on real appends."""
    every = am.consolidate_every()
    passes: list = []

    async def fake(command, prompt, timeout=0):
        passes.append(command)
        if len(passes) <= 2:                       # first pass only
            for i in range(every):
                am.append_history(EID, "a", "success", f"late {i}")
        return OK

    monkeypatch.setattr(knowledge, "_llm_call", fake)

    async def go():
        stub = _Stub()
        for i in range(every):
            am.append_history(EID, "a", "success", f"run {i}")
        stub._schedule_consolidate(EID, every)
        await _drain(stub)

    asyncio.run(go())
    assert len(passes) >= 4, "a full window arriving mid-flight must re-arm"
