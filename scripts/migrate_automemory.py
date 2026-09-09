"""One-shot migration into the layered auto-memory documents.

    .venv/bin/python scripts/migrate_automemory.py --dry-run
    .venv/bin/python scripts/migrate_automemory.py

No LLM is involved. Every decision here is deterministic, because an LLM pass
over the migration is exactly the step that produced the polluted documents this
migration is meant to leave behind.

  note × 22               -> profile.md, verbatim, date kept as a trailing
                             comment. These are all hand-written interaction
                             rules, which is precisely what profile.md is.
  knowledge_summary × 64  -> discarded. Each is a lossy one-line derivative
                             ending in "详见 …md", and 9 of the 16 source
                             documents are polluted, so importing them would
                             seed the feedback loop on day one.
  data/knowledge/*.md     -> left in place, read-only archive. Not imported:
                             8 documents in 8 shapes, and the polluted ones
                             would immediately be fed back as existing state.
  docs/error_experience.md -> seeds lessons.md for the agent-park eid. 41 lines,
                             10 numbered entries with occurrence counts, zero
                             pollution, human-reviewed — the highest-quality
                             experience artifact in the repo.

The original ``data/memory/{eid}.jsonl`` files are never modified: the layered
documents go into ``data/memory/{eid}/``, a sibling directory, so rollback is
just flipping ``automemory.enabled`` back to false.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import auto_memory as am  # noqa: E402

# error_experience.md documents agent-park's own mistakes, so it seeds the
# agent-park store rather than every eid.
ERROR_EXPERIENCE_EID = "f4bfb91dfc93"

PROFILE_HEADER = "<!-- Interaction rules. Authored by the user. Highest priority. -->"


def collect_notes() -> dict[str, list[tuple[str, str]]]:
    """Return {eid: [(content, date)]} from the flat jsonl files."""
    by_eid: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for path in sorted((ROOT / "data" / "memory").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "note":
                continue
            content = (entry.get("content") or "").strip()
            if content:
                by_eid[path.stem].append((content, (entry.get("timestamp") or "")[:10]))
    return by_eid


def render_profile(rows: list[tuple[str, str]]) -> str:
    lines = [PROFILE_HEADER, "# Profile", ""]
    for content, date in rows:
        lines.append(f"- {content}" + (f"  <!-- {date} -->" if date else ""))
    return "\n".join(lines) + "\n"


def parse_error_experience() -> list[dict]:
    """Parse docs/error_experience.md into lessons entries.

    Format, per numbered block:
        ### 1. Title (8次)
        <wrong description>
        → <right approach>
    """
    path = ROOT / "docs" / "error_experience.md"
    if not path.exists():
        return []
    entries: list[dict] = []
    blocks = re.split(r"\n(?=### \d+\.)", path.read_text(encoding="utf-8"))
    for block in blocks:
        m = re.match(r"### \d+\.\s*(?P<title>.+?)\s*(?:\((?P<n>\d+)\s*次\))?\s*$",
                     block.splitlines()[0] if block.splitlines() else "")
        if not m:
            continue
        title = m.group("title").strip()
        wrong_lines, right_lines = [], []
        for line in block.splitlines()[1:]:
            s = line.strip()
            if not s:
                continue
            if s.startswith("→"):
                right_lines.append(s.lstrip("→ ").strip())
            elif right_lines:
                right_lines.append(s)
            else:
                wrong_lines.append(s)
        body = []
        if wrong_lines:
            body.append(f"- 错误：{' '.join(wrong_lines)}")
        if right_lines:
            body.append(f"- 正确：{' '.join(right_lines)}")
        if not body:
            continue
        entries.append({
            "id": am.entry_id(title),
            "title": title,
            "n": int(m.group("n") or 1),
            "last": "",
            "body": body,
        })
    return entries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written, touch nothing")
    args = ap.parse_args()

    notes = collect_notes()
    lessons = parse_error_experience()

    planned: list[tuple[str, str, str]] = []  # (eid, layer, content)

    for eid, rows in sorted(notes.items()):
        planned.append((eid, "profile", render_profile(rows)))

    if lessons:
        kept, dropped = am.truncate_entries("lessons", lessons)
        planned.append((ERROR_EXPERIENCE_EID, "lessons",
                        am.render_entries("lessons", kept)))
        if dropped:
            print(f"note: {dropped} lesson(s) dropped by the size gate")

    # Refuse to clobber: a second run must not silently overwrite documents
    # that consolidation has since updated.
    skipped = []
    for eid, layer, _ in planned:
        if am.layer_path(eid, layer).exists():
            skipped.append(f"{eid}/{layer}.md")

    print(f"\n{'DRY RUN — nothing written' if args.dry_run else 'MIGRATING'}")
    print(f"  notes found:            {sum(len(v) for v in notes.values())} "
          f"across {len(notes)} eid(s)")
    print(f"  knowledge_summary:      discarded (lossy derivatives of polluted docs)")
    print(f"  error_experience.md:    {len(lessons)} lesson(s) -> {ERROR_EXPERIENCE_EID}")
    print(f"  data/knowledge/:        left untouched (read-only archive)")
    if skipped:
        print(f"\n  REFUSING to overwrite existing documents:")
        for s in skipped:
            print(f"    {s}")
        print("  Delete them first if you really want to re-migrate.")
        return 1

    for eid, layer, content in planned:
        path = am.layer_path(eid, layer)
        n_lines = len([l for l in content.splitlines() if l.startswith("- ") or l.startswith("## ")])
        print(f"\n  {path.relative_to(ROOT)}  ({len(content)} chars, {n_lines} item(s))")
        if args.dry_run:
            for line in content.splitlines()[:8]:
                print(f"    | {line[:96]}")
            if len(content.splitlines()) > 8:
                print(f"    | … {len(content.splitlines()) - 8} more line(s)")
        else:
            am.write_layer(eid, layer, content)

    if not args.dry_run:
        print(f"\nWrote {len(planned)} document(s). "
              f"The original *.jsonl files were not modified.")
        print("automemory.enabled is still false — injection is unchanged until you flip it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
