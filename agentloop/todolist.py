"""Todolist parsing and writing.

Schema (per DESIGN §4):

    ---
    project: <name>
    design_doc: design.md
    created_at: 2026-04-29T10:00:00Z
    cycle: 3
    ---

    # Todolist

    ## Items

    ### T-001 · type:dev · status:done
    实现 POST /foo 路由骨架
    - dependencies: []
    - dev_notes: ...

The parser is permissive on free-form body text but strict on the item header
and on the leading key-value bullets (``- key: value``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .workspace import WorkspacePaths

TODOLIST_FILE = "todolist.md"

_HEADER_RE = re.compile(
    r"^###\s+(?P<id>T-\d+)\s*·\s*type:(?P<type>[a-z\-]+)\s*·\s*status:(?P<status>[a-z_]+)\s*$"
)
_BULLET_RE = re.compile(r"^-\s+(?P<key>[a-zA-Z_]+):\s*(?P<value>.*)$")
_ATTEMPT_LINE_RE = re.compile(
    r"^\s*-\s*cycle\s+(?P<cycle>\d+):\s*(?P<result>[a-z_]+)"
    r"(?:\s*\((?P<notes>.*)\))?\s*$"
)

VALID_TYPES = {"dev", "qa", "design-update", "manual"}
VALID_STATUSES = {"pending", "doing", "ready_for_qa", "done", "abandoned"}


@dataclass
class Attempt:
    cycle: int
    result: str  # "ready_for_qa" | "pending"
    notes: str = ""

    def to_line(self) -> str:
        note_part = f" ({self.notes})" if self.notes else ""
        return f"  - cycle {self.cycle}: {self.result}{note_part}"


@dataclass
class Item:
    id: str
    type: str
    status: str
    title: str = ""
    dependencies: list[str] = field(default_factory=list)
    source: str | None = None
    dev_notes: str | None = None
    findings: str | None = None
    attempt_log: list[Attempt] = field(default_factory=list)

    def clone(self) -> "Item":
        return Item(
            id=self.id,
            type=self.type,
            status=self.status,
            title=self.title,
            dependencies=list(self.dependencies),
            source=self.source,
            dev_notes=self.dev_notes,
            findings=self.findings,
            attempt_log=[Attempt(a.cycle, a.result, a.notes) for a in self.attempt_log],
        )


@dataclass
class Todolist:
    """Container: front-matter metadata + ordered items."""

    metadata: dict[str, str] = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)

    def by_id(self, item_id: str) -> Item | None:
        for it in self.items:
            if it.id == item_id:
                return it
        return None

    def next_id(self) -> str:
        nums = []
        for it in self.items:
            try:
                nums.append(int(it.id.removeprefix("T-")))
            except ValueError:
                continue
        n = max(nums) + 1 if nums else 1
        return f"T-{n:03d}"


# ----- parse --------------------------------------------------------------


def parse(ws: WorkspacePaths) -> Todolist:
    path = ws.todolist
    if not path.exists():
        return Todolist()
    return parse_text(path.read_text(encoding="utf-8"))


def parse_text(text: str) -> Todolist:
    metadata, body = _split_frontmatter(text)
    items = _parse_items(body)
    return Todolist(metadata=metadata, items=items)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$", lines[idx])
        if m:
            meta[m.group(1)] = m.group(2).strip()
    if end is None:
        return {}, text
    body = "\n".join(lines[end + 1 :])
    return meta, body


def _parse_items(body: str) -> list[Item]:
    items: list[Item] = []
    current: Item | None = None
    current_lines: list[str] = []
    in_attempt_log = False

    for raw in body.splitlines():
        hdr = _HEADER_RE.match(raw)
        if hdr:
            if current is not None:
                _finalize_item(current, current_lines)
            current = Item(
                id=hdr.group("id"),
                type=hdr.group("type"),
                status=hdr.group("status"),
            )
            current_lines = []
            in_attempt_log = False
            continue
        if current is None:
            continue

        stripped = raw.rstrip()
        # attempt_log: indented "  - cycle N: ..." block
        if in_attempt_log and _ATTEMPT_LINE_RE.match(stripped):
            m = _ATTEMPT_LINE_RE.match(stripped)
            assert m is not None
            current.attempt_log.append(
                Attempt(
                    cycle=int(m.group("cycle")),
                    result=m.group("result"),
                    notes=(m.group("notes") or "").strip(),
                )
            )
            continue
        # any non-attempt line exits attempt_log mode
        if in_attempt_log and stripped.strip() == "":
            # keep iterating; attempt_log may continue after blank? we say no.
            in_attempt_log = False
        elif in_attempt_log:
            in_attempt_log = False

        bullet = _BULLET_RE.match(stripped)
        if bullet:
            key = bullet.group("key")
            value = bullet.group("value").strip()
            if key == "dependencies":
                current.dependencies = _parse_list(value)
            elif key == "source":
                current.source = value or None
            elif key == "dev_notes":
                current.dev_notes = value or None
            elif key == "findings":
                current.findings = value or None
            elif key == "attempt_log":
                in_attempt_log = True
            # unknown keys are ignored
            continue

        # section break
        if stripped.startswith("#"):
            # Unless it's the "### T-..." header (handled above) or the
            # "## Items" section, we treat it as end-of-item content.
            continue

        current_lines.append(stripped)

    if current is not None:
        _finalize_item(current, current_lines)
    return items if False else items  # placate linter


def _finalize_item(item: Item, content_lines: list[str]) -> None:
    # title = first non-empty line below the header
    for ln in content_lines:
        if ln.strip():
            item.title = ln.strip()
            break
    # appended to the (mutable) list via closure — but _parse_items builds its
    # own list; we'll just re-append below:
    # (see _parse_items for final assembly)
    # NOTE: caller is responsible for appending `item`; see loop below.


# The above design got a little tangled — rewrite a clean _parse_items that
# uses this helper correctly.


def _parse_items(body: str) -> list[Item]:  # noqa: F811
    items: list[Item] = []
    current: Item | None = None
    current_title_lines: list[str] = []
    in_attempt_log = False

    def flush():
        nonlocal current, current_title_lines, in_attempt_log
        if current is None:
            return
        for ln in current_title_lines:
            if ln.strip():
                current.title = ln.strip()
                break
        items.append(current)
        current = None
        current_title_lines = []
        in_attempt_log = False

    for raw in body.splitlines():
        hdr = _HEADER_RE.match(raw)
        if hdr:
            flush()
            current = Item(
                id=hdr.group("id"),
                type=hdr.group("type"),
                status=hdr.group("status"),
            )
            continue
        if current is None:
            continue

        stripped = raw.rstrip()

        if in_attempt_log:
            m = _ATTEMPT_LINE_RE.match(stripped)
            if m:
                current.attempt_log.append(
                    Attempt(
                        cycle=int(m.group("cycle")),
                        result=m.group("result"),
                        notes=(m.group("notes") or "").strip(),
                    )
                )
                continue
            # end of attempt_log block
            in_attempt_log = False

        bullet = _BULLET_RE.match(stripped)
        if bullet:
            key = bullet.group("key")
            value = bullet.group("value").strip()
            if key == "dependencies":
                current.dependencies = _parse_list(value)
            elif key == "source":
                current.source = value or None
            elif key == "dev_notes":
                current.dev_notes = value or None
            elif key == "findings":
                current.findings = value or None
            elif key == "attempt_log":
                in_attempt_log = True
            continue

        if stripped.startswith("##"):
            # treat section headers as non-title content
            continue

        current_title_lines.append(stripped)

    flush()
    return items


def _parse_list(value: str) -> list[str]:
    """Parse a bullet value like ``[T-001, T-003]`` or ``T-001, T-003``."""
    s = value.strip()
    if not s:
        return []
    s = s.strip("[]")
    if not s:
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


# ----- write --------------------------------------------------------------


def write(ws: WorkspacePaths, todolist: Todolist) -> None:
    text = render(todolist)
    path = ws.todolist
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def render(todolist: Todolist) -> str:
    out: list[str] = []
    if todolist.metadata:
        out.append("---")
        for key, value in todolist.metadata.items():
            out.append(f"{key}: {value}")
        out.append("---")
        out.append("")
    out.append("# Todolist")
    out.append("")
    out.append("## Items")
    out.append("")
    for item in todolist.items:
        out.extend(_render_item(item))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _render_item(item: Item) -> list[str]:
    lines = [f"### {item.id} · type:{item.type} · status:{item.status}"]
    if item.title:
        lines.append(item.title)
    # canonical field order
    if item.dependencies or item.type == "dev":
        lines.append(f"- dependencies: {_format_list(item.dependencies)}")
    if item.source:
        lines.append(f"- source: {item.source}")
    if item.dev_notes:
        lines.append(f"- dev_notes: {item.dev_notes}")
    if item.findings:
        lines.append(f"- findings: {item.findings}")
    if item.attempt_log:
        lines.append("- attempt_log:")
        for a in item.attempt_log:
            lines.append(a.to_line())
    return lines


def _format_list(values: Iterable[str]) -> str:
    vs = list(values)
    if not vs:
        return "[]"
    return "[" + ", ".join(vs) + "]"


# ----- helpers ------------------------------------------------------------


def trim_attempt_log(log: list[Attempt], max_keep: int = 3) -> list[Attempt]:
    """Keep first attempt + last (max_keep-1) attempts (per DESIGN §7)."""
    if len(log) <= max_keep:
        return list(log)
    return [log[0]] + list(log[-(max_keep - 1) :])
