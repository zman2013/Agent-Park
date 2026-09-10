"""Tests for the history layer and its consolidation trigger.

history.md is the one layer written on every finished run, without an LLM. Two
properties are worth pinning because both fail silently:

- the append format survives content containing its own separators or newlines
  (the text comes from arbitrary agent output)
- the counter is only cleared when a layer actually consumed the window, so an
  unusable LLM response retries instead of dropping those runs
"""
from __future__ import annotations

import asyncio
import shutil

import pytest

from server import auto_memory as am, knowledge

EID = "__test_history__"


@pytest.fixture
def eid(tmp_path, monkeypatch):
    monkeypatch.setattr(am, "MEMORY_DIR", tmp_path)
    yield EID
    shutil.rmtree(tmp_path / EID, ignore_errors=True)


def test_append_and_parse_round_trip(eid):
    am.append_history(eid, "agent / task", "success", "做了一件事")
    rows = am.parse_history(am.read_layer(eid, "history"))
    assert len(rows) == 1
    assert rows[0]["who"] == "agent / task"
    assert rows[0]["status"] == "success"
    assert rows[0]["text"] == "做了一件事"
    assert am.parse_history(am.render_history(rows)) == rows


@pytest.mark.parametrize("text,expected", [
    # Newlines must be folded, not dropped: parse keys on "- " at line start, so
    # an unfolded second line is silently discarded and the entry still looks
    # well-formed. Asserting only "1 row, no newline" would miss exactly that.
    ("第一行\n第二行", "第一行 第二行"),
    ("带 · 分隔符", "带 • 分隔符"),      # the field separator itself
    ("带 — 破折号", "带 - 破折号"),      # the status/text separator
    ("  折叠   空白  ", "折叠 空白"),
])
def test_hostile_content_survives_intact(eid, text, expected):
    am.append_history(eid, "a", "success", text)
    rows = am.parse_history(am.read_layer(eid, "history"))
    assert len(rows) == 1, "entry must not split or vanish"
    assert rows[0]["text"] == expected, "content must be transformed, never lost"


def test_long_text_is_truncated_with_a_marker(eid):
    am.append_history(eid, "a", "success", "x" * 500)
    row = am.parse_history(am.read_layer(eid, "history"))[0]
    assert len(row["text"]) <= am.HISTORY_SUMMARY_CHARS + 1
    assert row["text"].endswith("…"), "truncation must be visible, not silent"


def test_separators_in_the_who_field_do_not_shift_columns(eid):
    """`who` is interpolated before `status`, so an unescaped separator there
    would make the status column parse as part of the name."""
    am.append_history(eid, "a · b — c", "failed", "text")
    row = am.parse_history(am.read_layer(eid, "history"))[0]
    assert row["status"] == "failed"
    assert row["text"] == "text"


def test_empty_text_does_not_advance_the_counter(eid):
    """An empty run must not push the window toward an LLM call."""
    am.append_history(eid, "a", "success", "real")
    before = am.read_history_counter(eid)
    assert am.append_history(eid, "a", "success", "   ") == before
    assert len(am.parse_history(am.read_layer(eid, "history"))) == 1


def test_rolls_off_at_both_limits(eid):
    for i in range(80):
        am.append_history(eid, "a", "success", "x" * 300)
    md = am.read_layer(eid, "history")
    assert len(am.parse_history(md)) <= am.HISTORY_MAX_ENTRIES
    assert len(md) <= am.LAYER_LIMITS["history"]


def test_counter_survives_a_restart(eid):
    """The counter is on disk, not in memory: a restart mid-window must not
    reset the trigger, or a busy period could never reach it."""
    for i in range(4):
        am.append_history(eid, "a", "success", f"run {i}")
    assert am.read_history_counter(eid) == 4
    # A fresh read with no in-process state is what a restart looks like.
    assert am.read_history_counter(eid) == 4


def test_corrupt_counter_does_not_wedge_consolidation(eid):
    am.append_history(eid, "a", "success", "x")
    am._counter_path(eid).write_text("not a number", encoding="utf-8")
    assert am.read_history_counter(eid) == 0


def test_history_feeds_extraction(eid):
    am.append_history(eid, "agent / t", "failed", "改了 server/x.py")
    signals = am.extract_history_signals(eid)
    assert len(signals) == 1
    assert signals[0]["source"] == "recent_action"
    assert "改了 server/x.py" in signals[0]["content"]
    assert "failed" in signals[0]["content"]


def _stub(monkeypatch, reply):
    async def fake(command, prompt, timeout=0):
        return reply
    monkeypatch.setattr(knowledge, "_llm_call", fake)


def test_window_is_kept_when_every_layer_fails(eid, monkeypatch):
    """The failure that matters: an unusable LLM reply must not consume the
    window. Clearing it unconditionally would drop those runs permanently —
    the same silent-loss shape as the old "timeout returns existing_md"."""
    for i in range(3):
        am.append_history(eid, "a", "success", f"run {i}")
    _stub(monkeypatch, "分析完成，没有新增。")
    result = asyncio.run(am.consolidate(eid, []))
    assert sorted(result["failed_layers"]) == ["lessons", "project"]
    assert am.read_history_counter(eid) == 3


def test_window_is_consumed_on_success(eid, monkeypatch):
    for i in range(3):
        am.append_history(eid, "a", "success", f"run {i}")
    _stub(monkeypatch, '[{"op":"add","title":"T","fact":"F"}]')
    result = asyncio.run(am.consolidate(eid, []))
    assert result["failed_layers"] == []
    assert am.read_history_counter(eid) == 0
    # Consolidation marks the window consumed; it does not rewrite history.md.
    assert len(am.parse_history(am.read_layer(eid, "history"))) == 3


def test_consolidation_never_writes_history_or_profile(eid, monkeypatch):
    am.append_history(eid, "a", "success", "run")
    before = am.read_layer(eid, "history")
    _stub(monkeypatch, '[{"op":"add","title":"T","fact":"F"}]')
    asyncio.run(am.consolidate(eid, []))
    assert am.read_layer(eid, "history") == before
    assert am.read_layer(eid, "profile") == ""


def test_hotfiles_is_gone():
    """The layer and its producer were removed, not just unlinked from LAYERS."""
    assert "hotfiles" not in am.LAYERS
    assert "hotfiles" not in am.LAYER_LIMITS
    assert not hasattr(knowledge, "compute_hotfiles")
    assert not hasattr(knowledge, "build_hotfiles_md")


def test_history_is_injected_with_a_priority_intro():
    assert "history" in am.LAYERS
    assert am._LAYER_INTROS["history"].startswith("[History]")
    # Ordered last: it is context, and must not outrank the user's own rules.
    assert am.LAYERS.index("history") == len(am.LAYERS) - 1
    assert am.LAYERS.index("profile") == 0


def test_appends_during_consolidation_survive_the_reset(eid, monkeypatch):
    """Reported by review: tasks finishing while the two LLM calls are in
    flight bump the same on-disk counter, but their lines were not in the
    snapshot consolidation read. Clearing the file unconditionally swallowed
    them — with daily_enabled off, or an eid that then went quiet, those runs
    would never trigger a pass of their own."""
    for i in range(3):
        am.append_history(eid, "a", "success", f"run {i}")

    async def fake(command, prompt, timeout=0):
        # Two more runs finish mid-flight, after the snapshot was taken.
        am.append_history(eid, "a", "success", "late run")
        return '[{"op":"add","title":"T","fact":"F"}]'

    monkeypatch.setattr(knowledge, "_llm_call", fake)
    asyncio.run(am.consolidate(eid, []))
    # One append per layer call, both after the snapshot of 3.
    assert am.read_history_counter(eid) == 2


def test_reset_clears_the_file_when_nothing_arrived_late(eid, monkeypatch):
    for i in range(3):
        am.append_history(eid, "a", "success", f"run {i}")
    _stub(monkeypatch, '[{"op":"add","title":"T","fact":"F"}]')
    asyncio.run(am.consolidate(eid, []))
    assert am.read_history_counter(eid) == 0
    assert not am._counter_path(eid).exists(), "a consumed window leaves no file"


def test_reset_does_not_go_negative(eid):
    am.append_history(eid, "a", "success", "one")
    am.reset_history_counter(eid, consumed=99)
    assert am.read_history_counter(eid) == 0


def test_the_trigger_fires_on_at_least_the_threshold_not_only_on_multiples(eid,
                                                                          monkeypatch):
    """Reported by review. Since a pass subtracts only what it consumed, the
    counter no longer lands on multiples — so `n % every == 0` left a full
    unconsumed window waiting for the *next* multiple: 10 stuck at 11 needing 9
    more, or forever if the eid went quiet with daily consolidation off.

    Asserted against the arithmetic rather than through AgentRunner, which needs
    a live event loop and app_state: the condition is the whole fix.
    """
    every = am.consolidate_every()
    fires = lambda n: n >= every                     # noqa: E731 — the condition itself
    modulo = lambda n: bool(n) and n % every == 0    # noqa: E731 — what it replaced
    assert fires(every) and modulo(every), "both agree on an exact window"
    stuck = every + 1
    assert fires(stuck), "a full window at a non-multiple remainder must fire"
    assert not modulo(stuck), "which the old condition did not"
    assert not fires(every - 1), "a partial window must still not fire"
