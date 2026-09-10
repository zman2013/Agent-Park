"""The nightly loop must stamp entries with the day it consolidated.

Reported by review. ``_run_daily_summary`` deliberately selects the tasks of
*date* — normally the day that just ended — but omitting ``today=date`` let
``consolidate()`` fall back to the wall clock. Every nightly result was then
dated one day late, and a replay of old history was dated as the replay day.
``last`` feeds the truncation ranking, so wrong dates retain or discard the
wrong entries.

Only the date threading is exercised here: the whole path is patched down to
``consolidate``, because the surrounding scheduler owns sleeping until 00:30 and
has nothing to do with this property.
"""
from __future__ import annotations

import asyncio

import pytest

from server import routes_ws


class _Task:
    def __init__(self, tid, updated_at):
        self.id, self.updated_at, self.name = tid, updated_at, tid
        self.status, self.num_turns, self.messages = "success", 1, []


@pytest.fixture
def captured(monkeypatch):
    """Patch consolidate() and record the kwargs it was called with."""
    calls: list[dict] = []

    async def fake_consolidate(eid, tasks, progress_cb=None, today=None):
        calls.append({"eid": eid, "tasks": tasks, "today": today})
        return {"added": 0, "updated": 0, "deleted": 0, "refused": 0,
                "failed_layers": [], "layers": {}, "dropped": 0, "eid": eid}

    import server.auto_memory as am
    monkeypatch.setattr(am, "consolidate", fake_consolidate)
    return calls


def test_the_summarized_date_is_passed_into_consolidation(monkeypatch, captured):
    date = "2026-04-07"
    monkeypatch.setattr(routes_ws, "_eid_tasks",
                        lambda eid: [_Task("t1", f"{date}T21:00:00")])
    asyncio.run(routes_ws._run_daily_summary("eid1", date))
    assert len(captured) == 1
    assert captured[0]["today"] == date, \
        "entries would be stamped with the wall clock, not the day consolidated"


def test_no_tasks_for_the_date_skips_the_llm_entirely(monkeypatch, captured):
    monkeypatch.setattr(routes_ws, "_eid_tasks",
                        lambda eid: [_Task("t1", "2026-04-01T10:00:00")])
    asyncio.run(routes_ws._run_daily_summary("eid1", "2026-04-07"))
    assert captured == []
