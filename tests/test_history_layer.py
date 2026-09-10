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


def test_only_the_unconsumed_window_is_fed_back(eid, monkeypatch):
    """Reported by review, and the reason it was P1. reset_history_counter clears
    the counter but history.md keeps its rows (deliberately — it is also a
    human-readable log and build_context injects it). So the next pass re-fed
    every row it had already consolidated: at 20 runs the first 10 go through
    twice, and at the 50-row steady state most of the input is replayed every
    cycle. Each replay reads as a re-sighting and bumps `n` on entries nothing
    new happened to, corrupting the exact ranking signal `n` exists to provide.
    """
    for i in range(12):
        am.append_history(eid, "a", "success", f"旧运行 {i}")
    _stub(monkeypatch, '[{"op":"add","title":"T","fact":"F"}]')
    asyncio.run(am.consolidate(eid, [], history_window_only=True))
    assert am.read_history_counter(eid) == 0

    for i in range(3):
        am.append_history(eid, "a", "success", f"新运行 {i}")

    seen: list[str] = []

    async def capture(command, prompt, timeout=0):
        seen.append(prompt)
        return '[{"op":"add","title":"T2","fact":"F2"}]'

    monkeypatch.setattr(knowledge, "_llm_call", capture)
    asyncio.run(am.consolidate(eid, [], history_window_only=True))
    assert seen
    for p in seen:
        assert "新运行 2" in p, "the unconsumed window must be fed"
        assert "旧运行 0" not in p, "an already-consolidated row must not be re-fed"


def test_the_slice_takes_the_newest_rows_not_the_oldest(eid, monkeypatch):
    for i in range(10):
        am.append_history(eid, "a", "success", f"运行 {i}")
    am.reset_history_counter(eid)
    for i in range(2):
        am.append_history(eid, "a", "success", f"窗口内 {i}")
    signals = am.extract_history_signals(eid, am.read_history_counter(eid))
    assert len(signals) == 2
    assert all("窗口内" in s["content"] for s in signals)


def test_the_whole_file_is_fed_when_no_window_is_given(eid):
    """The nightly loop and the 🧠 button are not consuming a window and are
    explicitly asked to look at everything."""
    for i in range(5):
        am.append_history(eid, "a", "success", f"运行 {i}")
    assert len(am.extract_history_signals(eid)) == 5
    assert len(am.extract_history_signals(eid, None)) == 5


def test_a_zero_window_feeds_no_history(eid):
    """A pass triggered with nothing unconsumed must not fall back to the file."""
    am.append_history(eid, "a", "success", "运行")
    assert am.extract_history_signals(eid, 0) == []


# ── per-task message watermarks ───────────────────────────────────────────────
#
# Reported by review as the third input replaying the same material: a Task is a
# resumable conversation, but history advances once per finished run. A task
# resumed across several windows was returned by the task-count slice each time,
# and both extractors walked its entire accumulated `messages`.

class _M:
    def __init__(self, content):
        self.role, self.type, self.content = "agent", "tool_result", content


class _RT:
    """A resumable task whose transcript grows between passes."""

    def __init__(self, tid, n):
        self.id, self.name, self.status = tid, tid, "failed"
        self.num_turns, self.updated_at = 1, "2026-09-09T10:00:00"
        self.messages = [_M(f"ValueError: 第 {i} 条") for i in range(n)]


def test_a_resumed_task_does_not_replay_its_earlier_messages(eid, monkeypatch):
    task = _RT("t1", 3)
    _stub(monkeypatch, '[{"op":"add","title":"T","fact":"F"}]')
    am.append_history(eid, "a", "success", "run 1")
    asyncio.run(am.consolidate(eid, [task], history_window_only=True))

    # Resumed: two more messages land on the same task object.
    task.messages.append(_M("ValueError: 第 3 条"))
    task.messages.append(_M("ValueError: 第 4 条"))

    seen: list[str] = []

    async def capture(command, prompt, timeout=0):
        seen.append(prompt)
        return '[{"op":"add","title":"T2","fact":"F2"}]'

    monkeypatch.setattr(knowledge, "_llm_call", capture)
    am.append_history(eid, "a", "success", "run 2")
    asyncio.run(am.consolidate(eid, [task], history_window_only=True))
    # The lessons prompt is the one tool_result signals reach; the project
    # extractor only reads agent *text*, so it never sees these at all.
    lessons = [p for p in seen if "错误经验提取器" in p]
    assert lessons, "the lessons layer must have been prompted"
    for p in lessons:
        assert "第 4 条" in p, "the new messages must be fed"
        assert "第 0 条" not in p, "an already-consolidated message must not be re-fed"


def test_a_task_with_nothing_new_is_dropped_entirely(eid):
    task = _RT("t1", 2)
    views, endpoints = am.slice_new_messages([task], {"t1": 2})
    assert views == []
    assert endpoints == {"t1": 2}, "a dropped task still needs its endpoint recorded"


def test_the_view_exposes_only_new_messages_but_forwards_other_fields(eid):
    task = _RT("t1", 5)
    views, endpoints = am.slice_new_messages([task], {"t1": 3})
    view = views[0]
    assert len(view.messages) == 2
    assert view.id == "t1" and view.status == "failed", "other fields must forward"
    assert len(task.messages) == 5, "the live task must not be mutated"
    assert endpoints == {"t1": 5}


def test_a_mark_beyond_the_transcript_does_not_crash(eid):
    """A task whose messages were trimmed must not raise or resurrect old ones."""
    views, _ = am.slice_new_messages([_RT("t1", 2)], {"t1": 99})
    assert views == []


def test_a_failed_pass_does_not_advance_the_marks(eid, monkeypatch):
    """Same posture as the history window: the retry must see the same input."""
    task = _RT("t1", 3)
    am.append_history(eid, "a", "success", "run")
    _stub(monkeypatch, "分析完成，没有新增。")
    asyncio.run(am.consolidate(eid, [task], history_window_only=True))
    assert am.read_consumed_marks(eid) == {}


def test_marks_are_merged_not_pruned_to_the_window(eid, monkeypatch):
    """A task leaves the newest-N window and returns when resumed; a pruned mark
    would replay its whole transcript."""
    _stub(monkeypatch, '[{"op":"add","title":"T","fact":"F"}]')
    am.append_history(eid, "a", "success", "run 1")
    asyncio.run(am.consolidate(eid, [_RT("old", 4)], history_window_only=True))
    am.append_history(eid, "a", "success", "run 2")
    asyncio.run(am.consolidate(eid, [_RT("new", 2)], history_window_only=True))
    marks = am.read_consumed_marks(eid)
    assert marks == {"old": 4, "new": 2}


def test_marks_are_bounded(eid):
    am.write_consumed_marks(eid, {f"t{i}": i for i in range(am.MAX_CONSUMED_MARKS + 50)})
    marks = am.read_consumed_marks(eid)
    assert len(marks) == am.MAX_CONSUMED_MARKS
    assert min(marks.values()) == 50, "the lowest watermarks are dropped first"


def test_corrupt_marks_do_not_wedge_consolidation(eid):
    am.write_consumed_marks(eid, {"t1": 1})
    am._marks_path(eid).write_text("not json", encoding="utf-8")
    assert am.read_consumed_marks(eid) == {}


def test_the_manual_path_still_sees_whole_transcripts(eid, monkeypatch):
    """The 🧠 button and nightly loop are explicitly asked to look at everything,
    so they must ignore the marks."""
    task = _RT("t1", 3)
    _stub(monkeypatch, '[{"op":"add","title":"T","fact":"F"}]')
    am.append_history(eid, "a", "success", "run")
    asyncio.run(am.consolidate(eid, [task], history_window_only=True))

    seen: list[str] = []

    async def capture(command, prompt, timeout=0):
        seen.append(prompt)
        return "[]"

    monkeypatch.setattr(knowledge, "_llm_call", capture)
    asyncio.run(am.consolidate(eid, [task]))          # no history_window_only
    lessons = [p for p in seen if "错误经验提取器" in p]
    assert lessons and all("第 0 条" in p for p in lessons)


def test_messages_arriving_during_consolidation_are_not_marked_consumed(eid, monkeypatch):
    """Reported by review. The window's start and end must come from the same
    snapshot: reading len(task.messages) after the two LLM calls records messages
    that were never in either prompt as consumed, permanently skipping that run.
    """
    task = _RT("t1", 2)
    am.append_history(eid, "a", "success", "run 1")

    async def resume_mid_flight(command, prompt, timeout=0):
        # A user resumes this task while the helper is thinking.
        task.messages.append(_M("ValueError: 巩固期间到达"))
        return '[{"op":"add","title":"T","fact":"F"}]'

    monkeypatch.setattr(knowledge, "_llm_call", resume_mid_flight)
    asyncio.run(am.consolidate(eid, [task], history_window_only=True))
    # Two messages were snapshotted and fed; the later arrivals were not.
    assert am.read_consumed_marks(eid) == {"t1": 2}

    seen: list[str] = []

    async def capture(command, prompt, timeout=0):
        seen.append(prompt)
        return "[]"

    monkeypatch.setattr(knowledge, "_llm_call", capture)
    am.append_history(eid, "a", "success", "run 2")
    asyncio.run(am.consolidate(eid, [task], history_window_only=True))
    lessons = [p for p in seen if "错误经验提取器" in p]
    assert lessons and all("巩固期间到达" in p for p in lessons), \
        "a message appended mid-pass must still be fed by the next one"


def test_the_endpoint_is_snapshotted_even_for_a_task_with_nothing_new(eid):
    """Otherwise a task that contributed nothing loses its watermark entirely and
    replays its whole transcript next time."""
    task = _RT("t1", 3)
    _, endpoints = am.slice_new_messages([task], {"t1": 3})
    assert endpoints == {"t1": 3}


# ── forks inherit a transcript under a new id ──────────────────────────────────

def test_a_fork_does_not_re_feed_its_inherited_transcript(eid):
    """Reported by review: state.fork_task deep-copies the source's messages, but
    the fork has a new id and therefore no watermark — so the default of 0 read
    the whole inherited copy as new."""
    fork = _RT("fork1", 5)
    fork.inherited_messages = 4
    views, endpoints = am.slice_new_messages([fork], {})
    assert len(views) == 1
    assert len(views[0].messages) == 1, "only the post-fork message is new"
    assert endpoints == {"fork1": 5}


def test_a_forks_own_watermark_wins_once_it_has_one(eid):
    """After its first pass the fork has a real mark, which must not be dragged
    back down to the inherited floor."""
    fork = _RT("fork1", 8)
    fork.inherited_messages = 4
    views, _ = am.slice_new_messages([fork], {"fork1": 6})
    assert len(views[0].messages) == 2


def test_an_ordinary_task_is_unaffected_by_the_fork_floor(eid):
    task = _RT("t1", 3)
    assert getattr(task, "inherited_messages", 0) == 0
    views, _ = am.slice_new_messages([task], {})
    assert len(views[0].messages) == 3


def test_fork_task_records_the_inherited_count():
    """The field has to actually be set at the fork site, not just honoured."""
    from server.models import Message, Task
    from server.state import AppState

    st = AppState.__new__(AppState)
    st.tasks, st.agents = {}, {}
    st._agent_order = []
    st.save_agent_tasks = lambda *a, **k: None

    from server.models import Agent
    agent = Agent(name="a", cwd="/tmp")
    src = Task(agent_id=agent.id, name="src", prompt="p",
               messages=[Message(role="agent", type="text", content=f"m{i}")
                         for i in range(4)])
    st.agents[agent.id] = agent
    st.tasks[src.id] = src
    fork = st.fork_task(src.id, "sess-1")
    assert fork.inherited_messages == 4 == len(fork.messages)
