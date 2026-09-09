"""Tests for the two budget/format gates in apply_delta.

Both properties below were reported by review and reproduced before fixing, and
both fail *silently* — the document stays syntactically plausible, so nothing
raises and nothing logs unless you count the entries yourself:

- a newline in any delta field breaks the Markdown grammar the id markers live
  in, shifting or duplicating entries on the next parse
- the item cap was only stated in the prompt, so a model that ignored it (or a
  user who lowered the number) left the configured limit meaning nothing
"""
from __future__ import annotations

import pytest

from server import auto_memory as am


def _add(title: str, **fields) -> dict:
    return {"op": "add", "title": title, **fields}


def _apply(layer: str, ops: list[dict]):
    entries, res = am.apply_delta(layer, [], ops, "2026-09-09")
    return entries, res, am.render_entries(layer, entries)


# ── multiline fields ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("op", [
    # A newline in `title` pushes the generated <!-- id:… --> marker onto the
    # next line, where _ENTRY_RE stops matching a heading.
    _add("标题第一行\n## 伪造 <!-- id:aaaaaa -->", fact="事实"),
    # A newline in a body field can open a "## " heading of its own.
    _add("正常标题", fact="第一行\n## 伪造 <!-- id:bbbbbb -->"),
    _add("教训", wrong="错\n## 伪造", right="对\n## 伪造2"),
    _add("回车也算", fact="a\r\nb"),
])
def test_a_multiline_field_cannot_forge_an_entry(op):
    entries, res, md = _apply("project", [op])
    assert res["added"] == 1
    headings = [l for l in md.splitlines() if l.startswith("## ")]
    assert len(headings) == 1, f"one op must render exactly one heading: {headings}"
    parsed = am.parse_entries(md)
    assert len(parsed) == 1
    assert parsed[0]["id"] == entries[0]["id"], "the id marker moved off its heading"


def test_folding_preserves_content_rather_than_refusing_the_op():
    """The model said something valid and only formatted it wrongly, so the
    words must survive — dropping the op would lose a real lesson."""
    _, res, md = _apply("lessons", [_add("t", wrong="第一行\n第二行", right="对")])
    assert res["refused"] == 0
    assert "第一行 第二行" in md


def test_render_parse_is_idempotent_after_folding():
    _, _, md = _apply("project", [_add("a\nb", fact="c\nd")])
    once = am.parse_entries(md)
    assert am.parse_entries(am.render_entries("project", once)) == once


def test_a_whitespace_only_field_is_still_refused():
    """Folding must not turn an empty op into a valid one."""
    _, res, _ = _apply("project", [_add("  \n  ", fact="x"), _add("t", fact=" \n ")])
    assert res["added"] == 0
    assert res["refused"] == 2


# ── item cap ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("layer", ["lessons", "project"])
def test_item_cap_is_enforced_in_python_not_only_in_the_prompt(layer):
    cap = am.max_items_for(layer)
    entries, res, _ = _apply(layer, [
        _add(f"条目{i}", fact="x", wrong="w", right="r") for i in range(cap * 3)
    ])
    assert len(entries) == cap
    assert res["dropped"] == cap * 2


def test_the_char_ceiling_still_binds_below_the_item_cap():
    """The two ceilings are independent: a few fat entries must be dropped even
    when the item count is legal. Enforcing only the item cap would regress the
    original gate."""
    cap = am.max_items_for("project")
    fat = [{"id": f"{i:06x}", "title": f"t{i}", "n": 1, "last": "2026-01-01",
            "body": ["- " + "x" * 900]} for i in range(cap)]
    kept, dropped = am.truncate_entries("project", fat)
    assert dropped > 0
    assert len(am.render_entries("project", kept)) <= am.LAYER_LIMITS["project"]


def test_in_budget_documents_are_returned_untouched():
    rows = [{"id": "a" * 6, "title": "t", "n": 1, "last": "2026-01-01", "body": ["- x"]}]
    kept, dropped = am.truncate_entries("project", rows)
    assert kept is rows and dropped == 0


def test_the_cap_keeps_the_most_established_entries():
    """Ranking is n DESC — an over-cap batch must not drop by arrival order."""
    cap = am.max_items_for("project")
    rows = [{"id": f"{i:06x}", "title": f"t{i}", "n": i, "last": "2026-01-01",
             "body": ["- x"]} for i in range(cap + 5)]
    kept, dropped = am.truncate_entries("project", rows)
    assert dropped == 5
    assert min(e["n"] for e in kept) == 5, "the five lowest-n entries must go"
