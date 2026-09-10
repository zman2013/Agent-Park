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


# ── aggregate ceiling ─────────────────────────────────────────────────────────
#
# Reported by review: each POST is capped at 300 characters but nothing capped
# the total, and build_context() injects this file verbatim on every task start.
# Refusing is the honest option here — the other layers drop their weakest
# entries, but this one is the user's own writing, and silently discarding a line
# they typed is worse than telling them the file is full.

def test_appends_stop_at_the_layer_ceiling(tmp_path, monkeypatch):
    import pytest as _pytest

    from server import auto_memory as am
    from server import profile_store as ps

    monkeypatch.setattr(am, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr("server.auto_memory.effective_id", lambda a: "__test_profile__")

    limit = am.LAYER_LIMITS["profile"]
    added = 0
    with _pytest.raises(ps.ProfileFull):
        for i in range(200):
            ps.append_profile("agent1", f"规则{i}：" + "x" * 280)
            added += 1
    md = am.read_layer("__test_profile__", "profile")
    assert len(md) <= limit, f"{len(md)} chars would be injected on every task start"
    assert added > 0, "the ceiling must not reject the very first note"
    assert len(_parse(md)) == added, "a refused append must not be written"


def test_a_refused_append_leaves_the_file_byte_identical(tmp_path, monkeypatch):
    import pytest as _pytest

    from server import auto_memory as am
    from server import profile_store as ps

    monkeypatch.setattr(am, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr("server.auto_memory.effective_id", lambda a: "__test_profile2__")
    for i in range(6):
        ps.append_profile("agent1", f"规则{i}：" + "x" * 280)
    before = am.read_layer("__test_profile2__", "profile")
    with _pytest.raises(ps.ProfileFull):
        ps.append_profile("agent1", "压垮它的那一条：" + "y" * 280)
    assert am.read_layer("__test_profile2__", "profile") == before
