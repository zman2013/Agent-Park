#!/usr/bin/env python3
"""Wiki ingest entry script — processes completed tasks and merges knowledge into wikis.

Usage:
    cd /data1/common/agent-park
    python scripts/wiki_ingest.py [--date YYYY-MM-DD]

Processes all agents with a configured `wiki` field, extracts knowledge from
their successful tasks, and merges it into the corresponding wiki directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add project root to sys.path so server.* imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.state import app_state
from server.config import wiki_ingest_config


async def run(date: str | None = None) -> None:
    cfg = wiki_ingest_config()
    wiki_base = cfg["wiki_base"]

    # Collect agents with wiki configured
    agents_with_wiki = []
    for aid in app_state.ordered_agent_ids():
        agent = app_state.get_agent(aid)
        if not agent or agent.archived:
            continue
        if not agent.wiki:
            continue
        agents_with_wiki.append(agent)

    if not agents_with_wiki:
        print("No agents with wiki configured. Exiting.")
        return

    print(f"Found {len(agents_with_wiki)} agent(s) with wiki configured:")
    for a in agents_with_wiki:
        task_count = len(a.task_ids)
        print(f"  - {a.name} → wiki: {a.wiki} ({task_count} tasks)")

    total_processed = 0
    total_skipped = 0
    total_errors = 0

    for agent in agents_with_wiki:
        print(f"\n{'='*60}")
        print(f"Processing agent: {agent.name} (wiki: {agent.wiki})")
        print(f"{'='*60}")

        from server.wiki_ingest import ingest_agent_tasks

        result = await ingest_agent_tasks(agent.id, target_date=date)

        if "error" in result:
            print(f"  ERROR: {result['error']}")
            total_errors += 1
            continue

        if result.get("reason"):
            print(f"  SKIP: {result['reason']}")
            continue

        processed = result.get("tasks_processed", 0)
        skipped = result.get("tasks_skipped", 0)
        total_processed += processed
        total_skipped += skipped

        print(f"  Processed: {processed}, Skipped: {skipped}")

        for r in result.get("results", []):
            name = r.get("task_name", r.get("task_id", "?"))
            updates = r.get("updates", 0)
            files = r.get("files", [])
            error = r.get("error")
            if error:
                print(f"    ✗ {name}: {error}")
                total_errors += 1
            elif updates > 0:
                print(f"    ✓ {name}: {updates} update(s), files: {', '.join(files)}")
            else:
                print(f"    - {name}: no knowledge extracted")

    print(f"\n{'='*60}")
    print(f"Summary: {total_processed} tasks processed, {total_skipped} skipped, {total_errors} errors")


def main():
    parser = argparse.ArgumentParser(description="Wiki ingest: extract knowledge from tasks into wikis")
    parser.add_argument("--date", type=str, default=None, help="Only process tasks updated on this date (YYYY-MM-DD)")
    args = parser.parse_args()

    asyncio.run(run(date=args.date))


if __name__ == "__main__":
    main()
