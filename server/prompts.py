"""Global prompts storage — reads/writes data/prompts.jsonl."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
PROMPTS_FILE = DATA_DIR / "prompts.jsonl"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PROMPTS_FILE.exists():
        PROMPTS_FILE.touch()


def list_prompts() -> list[dict]:
    """Return all prompts, newest first."""
    _ensure_file()
    entries = []
    for line in PROMPTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return entries


def append_prompt(title: str, content: str) -> dict:
    """Append a new prompt and return the entry."""
    _ensure_file()
    entry = {
        "id": str(uuid.uuid4()),
        "title": title,
        "content": content,
        "created_at": _utcnow_iso(),
    }
    with PROMPTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def delete_prompt(prompt_id: str) -> bool:
    """Delete a prompt by id. Returns True if found and deleted."""
    _ensure_file()
    lines = PROMPTS_FILE.read_text(encoding="utf-8").splitlines()
    new_lines = []
    found = False
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue
        try:
            entry = json.loads(line_stripped)
        except json.JSONDecodeError:
            new_lines.append(line_stripped)
            continue
        if entry.get("id") == prompt_id:
            found = True
        else:
            new_lines.append(line_stripped)
    if found:
        PROMPTS_FILE.write_text(
            "\n".join(new_lines) + ("\n" if new_lines else ""),
            encoding="utf-8",
        )
    return found
