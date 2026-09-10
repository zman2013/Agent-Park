"""Tests for the two ceilings on a consolidation prompt's signal section.

The configured budget counts characters; the real constraint counts bytes. The
prompt reaches the helper LLM as one argv entry, and Linux caps a single entry at
MAX_ARG_STRLEN = 131072 bytes. Overflowing it raises E2BIG inside ``_llm_call``,
whose except clause returns "" — which every gate downstream reads as "no usable
output". So the failure mode is not a crash but a silently dead pipeline, the
exact shape this redesign existed to remove.
"""
from __future__ import annotations

import pytest

from server import auto_memory as am

MAX_ARG_STRLEN = 32 * 4096  # Linux, fixed at compile time


def _signals(n: int, text: str) -> list[dict]:
    return [{"source": "tool_error", "content": text} for _ in range(n)]


def test_the_char_budget_binds_on_ascii_input():
    out = am._format_signals(_signals(200, "x" * 800), 50_000)
    assert len(out) <= 50_000
    assert len(out) > 40_000, "should fill most of the budget, not stop early"


def test_the_byte_guard_binds_before_the_char_budget_on_cjk():
    """The property that matters: CJK input must be stopped by bytes while its
    character count is still far under the configured number. Asserting only
    "bytes <= guard" would also pass if the char budget happened to cut first."""
    out = am._format_signals(_signals(200, "中" * 800), 50_000)
    assert len(out.encode("utf-8")) <= am.MAX_SIGNAL_BYTES
    assert len(out) < 50_000, "the byte guard, not the char budget, must cut here"


def test_the_guard_leaves_room_for_the_rest_of_the_prompt():
    """Signals are not the whole prompt: the skeleton and the existing-entries
    JSON ride along uncapped. The guard has to leave headroom for both."""
    skeleton = am._PROMPTS["lessons"].format(existing="", signals="", max_items=0)
    worst = am.MAX_SIGNAL_BYTES + len(skeleton.encode("utf-8")) + 8_000
    assert worst < MAX_ARG_STRLEN, f"{worst} bytes would overflow a single argv"


@pytest.mark.parametrize("text", ["x" * 100, "中" * 100, "mixed 混合 text"])
def test_nothing_is_dropped_when_both_ceilings_are_slack(text):
    out = am._format_signals(_signals(5, text), 50_000)
    assert len(out.split("\n---\n")) == 5


def test_an_oversized_first_signal_yields_empty_rather_than_overflowing():
    """A single signal larger than the guard must be refused whole. Emitting it
    anyway would be the overflow this guard exists to prevent."""
    out = am._format_signals(_signals(1, "中" * 40_000), 50_000)
    assert len(out.encode("utf-8")) <= am.MAX_SIGNAL_BYTES


def test_flags_are_labelled_and_counted_against_the_budget():
    """The label is part of the argv payload, so it cannot be excluded from the
    accounting — a budget that ignores its own framing under-counts."""
    sig = [{"source": "tool_error", "content": "x" * 40, "task_failed": True}]
    out = am._format_signals(sig, 50_000)
    assert "[task_failed]" in out
    tight = am._format_signals(sig, 45)  # content alone fits, content+label does not
    assert tight == ""


# ── which signals survive the cut ─────────────────────────────────────────────
#
# Reported by review, and the reason it was P1: _format_signals keeps a *prefix*.
# The history-triggered path hands consolidate() every task the eid ever ran, in
# stored (oldest-first) order, with history_signals appended last. On an eid with
# enough backlog to fill the budget, the ten runs that just fired the trigger were
# cut in favour of the same ancient tasks — every time. The trigger fired forever
# and never saw its own window.

class _Msg:
    def __init__(self, content):
        self.role, self.type, self.content = "agent", "tool_result", content


class _Task:
    def __init__(self, tid, updated_at, content):
        self.id, self.updated_at, self.name = tid, updated_at, tid
        self.status, self.num_turns = "failed", 1
        self.messages = [_Msg(content)]


def test_consolidation_sees_the_newest_tasks_when_the_budget_binds(tmp_path, monkeypatch):
    """Oldest-first input plus a binding budget must still reach the newest run."""
    import asyncio

    from server import knowledge

    monkeypatch.setattr(am, "MEMORY_DIR", tmp_path)
    # Enough oldest-first bulk to overflow any reasonable prefix on its own.
    tasks = [_Task(f"old{i}", f"2026-01-{i % 28 + 1:02d}", "ValueError: boom " + "x" * 3000)
             for i in range(60)]
    tasks.append(_Task("newest", "2026-09-09", "ValueError: 最新那次运行 " + "y" * 3000))

    seen: list[str] = []

    async def fake(command, prompt, timeout=0):
        seen.append(prompt)
        return "[]"

    monkeypatch.setattr(knowledge, "_llm_call", fake)
    asyncio.run(am.consolidate("__test_order__", tasks, today="2026-09-09"))
    assert seen, "consolidation must have called the LLM"
    assert any("最新那次运行" in p for p in seen), \
        "the newest task was crowded out by the oldest ones"


def test_the_history_window_is_never_the_part_that_gets_cut(tmp_path, monkeypatch):
    """History is what the run is about to mark consumed, so losing it to the
    prefix cut means those appends are discarded unread."""
    import asyncio

    from server import knowledge

    monkeypatch.setattr(am, "MEMORY_DIR", tmp_path)
    eid = "__test_order2__"
    am.append_history(eid, "agent / t", "success", "刚刚完成的那件事")
    # Enough task bulk to exhaust the budget on its own, so history only
    # survives by being ordered ahead of it rather than by luck.
    tasks = [_Task(f"old{i}", f"2026-01-{i % 28 + 1:02d}", "ValueError: boom " + "x" * 3000)
             for i in range(400)]

    seen: list[str] = []

    async def fake(command, prompt, timeout=0):
        seen.append(prompt)
        return "[]"

    monkeypatch.setattr(knowledge, "_llm_call", fake)
    asyncio.run(am.consolidate(eid, tasks, today="2026-09-09"))
    assert all("刚刚完成的那件事" in p for p in seen), \
        "the history window must reach every layer's prompt"
