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


def test_the_module_agrees_with_the_kernel_on_the_argv_limit():
    """MAX_ARG_BYTES is the only place this number is written down in the module.

    This assertion used to try to prove the *fixed* 100_000 guard left room for
    the rest of the prompt, using a hand-picked 8_000 for the existing-entries
    JSON. That estimate was the bug: a full CJK document serializes to 41 KB, so
    the real worst case was 142 KB. The property is now proved by assembling the
    prompt (see test_the_worst_case_prompt_fits_a_single_argv) instead of by
    arithmetic on a guessed constant.
    """
    assert am.MAX_ARG_BYTES == MAX_ARG_STRLEN


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


# ── the guard must be sized against the whole argv ────────────────────────────
#
# Reported by review and reproduced by measurement: the guard was a fixed
# 100_000 bytes, on the assumption that the skeleton plus existing-entries JSON
# fit in the remaining 30 KB. 40 CJK project entries at the field ceiling
# serialize to 40.9 KB, putting the worst case at 142 KB — over MAX_ARG_BYTES,
# so _llm_call raised E2BIG and returned "" for every retry, wedging that
# document permanently.

def _worst_entries(layer: str) -> list[dict]:
    """max_items entries, every field CJK and at its ceiling."""
    n = am.max_items_for(layer)
    body = ([f"- 错误：{'错' * am.FIELD_LIMITS['body']}",
             f"- 正确：{'对' * am.FIELD_LIMITS['body']}"] if layer == "lessons"
            else [f"- {'实' * am.FIELD_LIMITS['body']}"])
    return [{"id": f"{i:06x}", "title": "标" * am.FIELD_LIMITS["title"], "n": 1,
             "last": "2026-01-01", "body": body} for i in range(n)]


@pytest.mark.parametrize("layer", ["lessons", "project"])
def test_the_worst_case_prompt_fits_a_single_argv(layer):
    """The property that actually matters: assembled, not just the signal part."""
    entries = _worst_entries(layer)
    existing = am._existing_for_prompt(layer, entries)
    max_items = am.max_items_for(layer)
    budget = am.signal_byte_budget(layer, existing, max_items)
    signals = am._format_signals(_signals(4000, "中" * 800), 10**9, budget)
    prompt = am._PROMPTS[layer].format(existing=existing, signals=signals,
                                       max_items=max_items)
    assert len(prompt.encode("utf-8")) <= am.MAX_ARG_BYTES, \
        f"{len(prompt.encode('utf-8'))} bytes would raise E2BIG in _llm_call"


@pytest.mark.parametrize("layer", ["lessons", "project"])
def test_a_full_document_still_leaves_room_for_signals(layer):
    """Shrinking the budget must not shrink it to nothing: a document that
    consolidates against no input reads as 'the model found nothing' forever."""
    budget = am.signal_byte_budget(
        layer, am._existing_for_prompt(layer, _worst_entries(layer)),
        am.max_items_for(layer))
    assert budget >= am.MIN_SIGNAL_BYTES


def test_an_empty_document_gets_the_full_budget():
    budget = am.signal_byte_budget("project", "（暂无）", 40)
    assert budget == am.MAX_SIGNAL_BYTES


def test_the_join_separators_are_counted():
    """5 bytes each — 2 KB across 400 signals, which is what pushed the earlier
    arithmetic over the line it thought it was under."""
    out = am._format_signals(_signals(400, "x" * 100), 10**9, 20_000)
    assert len(out.encode("utf-8")) <= 20_000
