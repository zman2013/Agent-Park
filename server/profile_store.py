"""profile.md read/write in the legacy note shape.

``profile.md`` is the one human-authored layer: consolidation never writes it.
The memory panel has always been an editor for exactly this content, and it
speaks ``[{type, timestamp, content, line_index}]``, so this module translates
between that shape and the Markdown bullets on disk.

Kept out of ``auto_memory`` because that module is on the injection hot path —
every task start imports it — and this is only reached from three REST handlers.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# "- text  <!-- 2026-04-07 -->"; the date is a trailing comment so the bullet
# stays readable as plain Markdown when a human edits the file directly.
_LINE_RE = re.compile(r"^-\s+(?P<content>.*?)(?:\s*<!--\s*(?P<date>[\d-]+)\s*-->)?\s*$")

_HEADER = "<!-- Interaction rules. Authored by the user. Highest priority. -->"


def _parse(md: str) -> list[tuple[str, str]]:
    """Return [(content, date)] for each bullet, in file order."""
    out = []
    for line in md.splitlines():
        if not line.startswith("- "):
            continue
        m = _LINE_RE.match(line)
        if m and m.group("content").strip():
            out.append((m.group("content").strip(), m.group("date") or ""))
    return out


def _render(rows: list[tuple[str, str]]) -> str:
    lines = [_HEADER, "# Profile", ""]
    for content, date in rows:
        lines.append(f"- {content}" + (f"  <!-- {date} -->" if date else ""))
    return "\n".join(lines) + "\n"


def list_profile(agent_id: str) -> list[dict]:
    """Return profile bullets newest-first, in the legacy note shape."""
    from server.auto_memory import effective_id, read_layer

    rows = _parse(read_layer(effective_id(agent_id), "profile"))
    entries = [
        {"type": "note", "timestamp": date, "content": content, "line_index": i}
        for i, (content, date) in enumerate(rows)
    ]
    entries.reverse()
    return entries


def append_profile(agent_id: str, content: str) -> None:
    from datetime import datetime, timezone

    from server.auto_memory import effective_id, read_layer, write_layer

    eid = effective_id(agent_id)
    rows = _parse(read_layer(eid, "profile"))
    rows.append((content, datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    write_layer(eid, "profile", _render(rows))


def delete_profile_line(agent_id: str, line_index: int) -> bool:
    from server.auto_memory import effective_id, read_layer, write_layer

    eid = effective_id(agent_id)
    rows = _parse(read_layer(eid, "profile"))
    if line_index < 0 or line_index >= len(rows):
        return False
    del rows[line_index]
    write_layer(eid, "profile", _render(rows))
    return True
