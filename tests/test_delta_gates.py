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

@pytest.mark.parametrize("layer,op", [
    # A newline in `title` pushes the generated <!-- id:… --> marker onto the
    # next line, where _ENTRY_RE stops matching a heading.
    ("project", _add("标题第一行\n## 伪造 <!-- id:aaaaaa -->", fact="事实")),
    # A newline in a body field can open a "## " heading of its own.
    ("project", _add("正常标题", fact="第一行\n## 伪造 <!-- id:bbbbbb -->")),
    # wrong/right is the lessons schema, so this case must be applied to that
    # layer: since the schema gate, a project op carrying wrong/right is refused
    # rather than written as error bullets into the project document.
    ("lessons", _add("教训", wrong="错\n## 伪造", right="对\n## 伪造2")),
    ("project", _add("回车也算", fact="a\r\nb")),
])
def test_a_multiline_field_cannot_forge_an_entry(layer, op):
    entries, res, md = _apply(layer, [op])
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


# ── per-field ceilings ────────────────────────────────────────────────────────
#
# The destructive case, reported by review: an `update` whose body exceeds the
# layer limit made the entry unfittable, so gate 4 dropped it — and the write
# still happened because the op counted as `updated`. Net effect of one
# malformed reply: a previously valid entry silently gone.

def test_an_oversized_update_does_not_destroy_the_entry_it_replaces():
    existing = [{"id": am.entry_id("原标题"), "title": "原标题", "n": 3,
                 "last": "2026-01-01", "body": ["- 原有事实"]}]
    entries, res = am.apply_delta("project", existing, [
        {"op": "update", "id": existing[0]["id"], "title": "原标题",
         "fact": "x" * (am.LAYER_LIMITS["project"] + 500)},
    ], "2026-09-09")
    assert res["refused"] == 1 and res["updated"] == 0
    assert res["dropped"] == 0, "the entry must not be dropped to make room"
    assert len(entries) == 1
    assert entries[0]["body"] == ["- 原有事实"], "the original body must survive"
    assert entries[0]["n"] == 3 and entries[0]["last"] == "2026-01-01"


@pytest.mark.parametrize("op", [
    _add("t" * (am.FIELD_LIMITS["title"] + 1), fact="f"),
    _add("t", fact="f" * (am.FIELD_LIMITS["body"] + 1)),
    _add("t", wrong="w" * (am.FIELD_LIMITS["body"] + 1), right="r"),
    _add("t", wrong="w", right="r" * (am.FIELD_LIMITS["body"] + 1)),
])
def test_oversized_fields_are_refused_not_truncated(op):
    """Refused, so it shows up in `refused` and the content is not presented as
    a fact in fragment form."""
    entries, res, _ = _apply("project", [op])
    assert entries == [] and res["refused"] == 1 and res["added"] == 0


def test_fields_at_the_ceiling_are_accepted():
    entries, res, _ = _apply("project", [
        _add("t" * am.FIELD_LIMITS["title"], fact="f" * am.FIELD_LIMITS["body"]),
    ])
    assert res["added"] == 1 and res["refused"] == 0


@pytest.mark.parametrize("layer", ["lessons", "project"])
def test_a_max_size_entry_always_fits_its_layer_alone(layer):
    """This is what removes the drop-on-update path: no field-legal entry can
    be too big for its own layer, so gate 4 never has to discard one."""
    e = {"id": "a" * 6, "title": "t" * am.FIELD_LIMITS["title"], "n": 1,
         "last": "2026-01-01",
         "body": [f"- 错误：{'w' * am.FIELD_LIMITS['body']}",
                  f"- 正确：{'r' * am.FIELD_LIMITS['body']}"]}
    kept, dropped = am.truncate_entries(layer, [e])
    assert dropped == 0 and len(kept) == 1


def test_the_field_gate_does_not_block_deletes():
    """A delete carries no content fields; refusing it on field size would make
    an oversized entry unremovable."""
    existing = [{"id": "abc123", "title": "t", "n": 1, "last": "2026-01-01",
                 "body": ["- " + "x" * 5000]}]
    entries, res = am.apply_delta("project", existing,
                                  [{"op": "delete", "id": "abc123"}], "2026-09-09")
    assert res["deleted"] == 1 and entries == []


# ── layer schema ──────────────────────────────────────────────────────────────
#
# Reported by review: _op_body sniffed which fields were present rather than
# keying on the layer, so a retry model answering in the *other* layer's schema
# was accepted. Both directions produce a structurally valid, semantically wrong
# entry, and both slipped past the completeness gate.

def test_a_project_op_in_the_lessons_schema_is_refused():
    """wrong/right written into project.md would render error bullets as
    project facts."""
    entries, res, _ = _apply("project", [_add("标题", wrong="错", right="对")])
    assert entries == [] and res["refused"] == 1 and res["added"] == 0


def test_a_lessons_op_in_the_project_schema_is_refused():
    """A lesson with no wrong/right is not a lesson."""
    entries, res, _ = _apply("lessons", [_add("标题", fact="一个事实")])
    assert entries == [] and res["refused"] == 1 and res["added"] == 0


def test_a_lessons_op_missing_half_the_pair_is_refused():
    for op in (_add("t", wrong="错"), _add("t", right="对")):
        entries, res, _ = _apply("lessons", [op])
        assert entries == [] and res["refused"] == 1


def test_each_layer_accepts_its_own_schema():
    _, res, md = _apply("lessons", [_add("t", wrong="错", right="对")])
    assert res["added"] == 1 and "- 错误：错" in md and "- 正确：对" in md
    _, res, md = _apply("project", [_add("t", fact="事实")])
    assert res["added"] == 1 and "- 事实" in md


def test_extra_fields_from_the_other_schema_are_ignored_not_rendered():
    """A project op that also carries wrong/right must keep only `fact`:
    rendering both would mix the two layers' vocabularies in one entry."""
    _, res, md = _apply("project", [_add("t", fact="事实", wrong="错", right="对")])
    assert res["added"] == 1
    assert "- 事实" in md and "错误：" not in md and "正确：" not in md


def test_an_oversized_update_in_the_wrong_schema_still_spares_the_entry():
    """The two new gates compose: a wrong-schema op must not delete what it
    was trying to replace either."""
    existing = [{"id": am.entry_id("原标题"), "title": "原标题", "n": 2,
                 "last": "2026-01-01", "body": ["- 原有事实"]}]
    _, res = am.apply_delta("project", existing, [
        {"op": "update", "id": existing[0]["id"], "title": "原标题",
         "wrong": "错", "right": "对"},
    ], "2026-09-09")
    assert res["refused"] == 1 and res["updated"] == 0
    assert existing[0]["body"] == ["- 原有事实"] and existing[0]["n"] == 2


# ── truncation is not an accepted change ──────────────────────────────────────
#
# Reported by review. The write condition included `dropped`, which contradicted
# its own comment: truncation is our gate firing, not a validated operation. An
# already-over-budget document (a lowered item cap, or a hand edit) plus a delta
# whose every op was refused was written anyway — so one `update` with an invented
# id could delete real entries purely by tripping the size gate.

def _over_budget(layer: str, n: int) -> list[dict]:
    return [{"id": f"{i:06x}", "title": f"t{i}", "n": 1, "last": "2026-01-01",
             "body": ["- x"]} for i in range(n)]


def test_an_all_refused_delta_never_writes(tmp_path, monkeypatch):
    """The destructive case: an invented id must not cost real entries."""
    import asyncio

    from server import auto_memory, knowledge

    monkeypatch.setattr(auto_memory, "MEMORY_DIR", tmp_path)
    eid = "__test_refuse__"
    cap = am.max_items_for("project")
    over = _over_budget("project", cap + 5)
    auto_memory.write_layer(eid, "project", am.render_entries("project", over))
    before = auto_memory.read_layer(eid, "project")

    async def fake(command, prompt, timeout=0):
        # Syntactically valid, every op refused: an id we never wrote.
        return '[{"op":"update","id":"ffffff","title":"t","fact":"f"}]'

    monkeypatch.setattr(knowledge, "_llm_call", fake)
    res = asyncio.run(auto_memory.consolidate_layer(
        eid, "project", [{"source": "s", "content": "c"}], "2026-09-09"))
    assert res["refused"] == 1
    assert res["added"] == res["updated"] == res["deleted"] == 0
    assert auto_memory.read_layer(eid, "project") == before, \
        "an over-budget document must not be truncated on a refused delta"
    assert len(am.parse_entries(auto_memory.read_layer(eid, "project"))) == cap + 5


def test_truncation_still_rides_along_with_an_accepted_change(tmp_path, monkeypatch):
    """The gate must not stop working: one real add on an over-budget document
    still trims it."""
    import asyncio

    from server import auto_memory, knowledge

    monkeypatch.setattr(auto_memory, "MEMORY_DIR", tmp_path)
    eid = "__test_trunc__"
    cap = am.max_items_for("project")
    auto_memory.write_layer(eid, "project",
                            am.render_entries("project", _over_budget("project", cap + 5)))

    async def fake(command, prompt, timeout=0):
        return '[{"op":"add","title":"崭新条目","fact":"事实"}]'

    monkeypatch.setattr(knowledge, "_llm_call", fake)
    res = asyncio.run(auto_memory.consolidate_layer(
        eid, "project", [{"source": "s", "content": "c"}], "2026-09-09"))
    assert res["added"] == 1 and res["dropped"] > 0
    assert len(am.parse_entries(auto_memory.read_layer(eid, "project"))) == cap
