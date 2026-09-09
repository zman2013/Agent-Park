"""Tests for the profile.md ↔ legacy-note shape translation.

The one contract worth pinning: an entry's text must survive a render/parse
round-trip. ``POST /api/agents/{id}/memory`` accepts free-form user input, and
the on-disk format is one Markdown bullet per entry — so without a continuation
convention a two-line rule loses everything after line 1, silently, along with
the date comment that sits on line 1's tail.
"""
from __future__ import annotations

import pytest

from server.profile_store import _parse, _render

DATE = "2026-09-09"


@pytest.mark.parametrize("content", [
    "使用中文与用户交互",
    "规则第一行\n规则第二行",
    "a\nb\nc",
    "顶层\n  缩进两格\n    缩进四格",
    "用 <!-- x --> 注释",
    "规则:\n- 子项 A\n- 子项 B",
])
def test_round_trip(content):
    assert _parse(_render([(content, DATE)])) == [(content, DATE)]


def test_blank_lines_inside_an_entry_are_not_preserved():
    """Documented limitation, asserted so it stays a decision and not a bug.

    Keeping them would mean emitting indent-only lines, which most editors strip
    on save — profile.md is meant to be hand-editable, so the format cannot
    honestly promise to round-trip them.
    """
    assert _parse(_render([("第一行\n\n第三行", DATE)])) == [("第一行\n第三行", DATE)]


def test_entries_keep_file_order_and_dates():
    rows = [("甲", "2026-01-01"), ("乙\n乙的第二行", "2026-02-02"), ("丙", "")]
    assert _parse(_render(rows)) == rows


def test_continuation_without_a_preceding_bullet_is_ignored():
    """Prose above the first bullet must not become an entry's continuation."""
    md = "<!-- header -->\n# Profile\n  这是散文，不是条目\n\n- 真条目\n"
    assert _parse(md) == [("真条目", "")]
