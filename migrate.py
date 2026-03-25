#!/usr/bin/env python3
"""One-time migration: split data/tasks.json into data/agents.json + data/tasks/{agent_id}.json.

Idempotent: safe to run multiple times.
- If data/tasks.json does not exist, exits immediately.
- If data/agents.json already exists, exits (migration already done).
- On success, renames data/tasks.json → data/tasks.json.bak.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
TASKS_FILE = DATA_DIR / "tasks.json"
AGENTS_FILE = DATA_DIR / "agents.json"
TASKS_DIR = DATA_DIR / "tasks"


def main() -> None:
    if not TASKS_FILE.exists():
        print("data/tasks.json not found — nothing to migrate.")
        return

    if AGENTS_FILE.exists():
        print("data/agents.json already exists — migration already done, skipping.")
        return

    print(f"Reading {TASKS_FILE} ({TASKS_FILE.stat().st_size / 1024 / 1024:.1f} MB)...")
    raw = json.loads(TASKS_FILE.read_text(encoding="utf-8"))

    agent_order = raw.get("agent_order", [])
    agents = raw.get("agents", {})
    tasks = raw.get("tasks", {})

    # Write agents.json
    agents_payload = {
        "agent_order": agent_order,
        "agents": agents,
    }
    tmp = AGENTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(agents_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(AGENTS_FILE)
    print(f"Written {AGENTS_FILE} ({len(agents)} agents)")

    # Group tasks by agent_id
    tasks_by_agent: dict[str, dict] = {}
    skipped = 0
    for tid, tdata in tasks.items():
        agent_id = tdata.get("agent_id")
        if not agent_id or agent_id not in agents:
            skipped += 1
            continue
        tasks_by_agent.setdefault(agent_id, {})[tid] = tdata

    # Write tasks/{agent_id}.json
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    for agent_id, agent_tasks in tasks_by_agent.items():
        task_file = TASKS_DIR / f"{agent_id}.json"
        payload = {"tasks": agent_tasks}
        tmp = task_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(task_file)
        size_kb = task_file.stat().st_size / 1024
        print(f"  Written {task_file.name} ({len(agent_tasks)} tasks, {size_kb:.0f} KB)")

    if skipped:
        print(f"  Skipped {skipped} tasks with unknown agent_id")

    # Rename original file as backup
    bak = TASKS_FILE.with_suffix(".json.bak")
    TASKS_FILE.rename(bak)
    print(f"\nBackup: {TASKS_FILE.name} → {bak.name}")
    print("Migration complete.")


if __name__ == "__main__":
    main()
