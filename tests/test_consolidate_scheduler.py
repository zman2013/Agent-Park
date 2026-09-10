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
