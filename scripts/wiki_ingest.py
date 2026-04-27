#!/usr/bin/env python3
"""Wiki ingest entry script — processes completed tasks and merges knowledge into wikis.

Usage:
    # Batch mode (default): process all agents with wiki configured
    python scripts/wiki_ingest.py [--date YYYY-MM-DD]

    # Single task mode: process one task by session ID or task ID
    python scripts/wiki_ingest.py --task <sessionId_or_taskId> [--force] [--wiki <name>]

Batch mode processes all agents with a configured `wiki` field, extracts knowledge
from their successful tasks, and merges it into the corresponding wiki directory.

Single task mode locates a task by session ID or task ID, auto-derives the wiki name
from agent config (agent.wiki → agent.cwd last dir → default "compiler"), and
processes only that one task.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

# Add project root to sys.path so server.* imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.config import wiki_ingest_config
from server.state import app_state
from server.wiki_ingest import (
    _wiki_dir, load_ingested, save_ingested, ensure_wiki_structure,
    ingest_task, ingest_agent_tasks,
    maybe_trigger_memforge_reindex,
)

DATA_DIR = PROJECT_ROOT / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"


def _require_wiki_base(cfg: dict) -> str:
    """Return wiki_base from config, or abort with a clear error if missing."""
    base = (cfg.get("wiki_base") or "").strip()
    if not base:
        print(
            "Error: wiki_ingest.wiki_base is not configured in config.json. "
            "Set it to the directory that holds your wikis "
            "(e.g. \"/data1/common/wiki\").",
            file=sys.stderr,
        )
        sys.exit(2)
    return base


# ── Single task helpers ───────────────────────────────────────────────────────

def _load_sessions() -> dict[str, str]:
    """Load {task_id: session_id} mapping from disk."""
    if not SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_task_agent_map() -> tuple[dict[str, str], dict[str, dict]]:
    """Build task→agent_id map and agent_id→metadata map from persisted files."""
    task_agent_map: dict[str, str] = {}
    tasks_dir = DATA_DIR / "tasks"
    if tasks_dir.exists():
        for tf in tasks_dir.glob("*.json"):
            try:
                data = json.loads(tf.read_text(encoding="utf-8"))
                for tid, tdata in data.get("tasks", {}).items():
                    task_agent_map[tid] = tdata.get("agent_id", "")
            except Exception:
                pass

    agent_map: dict[str, dict] = {}
    agents_file = DATA_DIR / "agents.json"
    if agents_file.exists():
        try:
            agents = json.loads(agents_file.read_text(encoding="utf-8"))
            agent_map = agents.get("agents", {})
        except Exception:
            pass

    return task_agent_map, agent_map


def resolve_task(session_or_task_id: str) -> tuple[str | None, str, str, str]:
    """Resolve session_id or task_id to (task_id, wiki_name, agent_name, agent_cwd).

    Wiki name derivation: agent.wiki → cwd last dir → "compiler".
    """
    task_agent_map, agent_map = _build_task_agent_map()

    def _resolve(tid: str):
        agent_id = task_agent_map.get(tid, "")
        agent = agent_map.get(agent_id, {})
        wiki = agent.get("wiki", "")
        cwd = agent.get("cwd", "")
        if not wiki and cwd:
            wiki = Path(cwd).name
        return tid, wiki or "compiler", agent.get("name", ""), cwd

    # Try as task_id first
    if session_or_task_id in task_agent_map or app_state.get_task(session_or_task_id):
        return _resolve(session_or_task_id)

    # Try as session_id
    sessions = _load_sessions()
    for tid, sid in sessions.items():
        if sid == session_or_task_id:
            return _resolve(tid)

    return None, "", "", ""


def _resolve_wiki(agent: dict | None) -> str:
    """Derive wiki name from agent config."""
    if not agent:
        return "compiler"
    wiki = agent.wiki
    if not wiki and agent.cwd:
        wiki = Path(agent.cwd).name
    return wiki or "compiler"


async def run_single_task(session_or_task_id: str, force: bool = False, wiki_override: str | None = None) -> None:
    """Process a single task by session ID or task ID."""
    from server.config import wiki_ingest_config as _wiki_cfg

    task_id, auto_wiki, agent_name, agent_cwd = resolve_task(session_or_task_id)

    if task_id is None:
        print(f"Error: session/task {session_or_task_id} not found")
        sys.exit(1)

    task = app_state.get_task(task_id)
    if task is None:
        print(f"Error: task {task_id} not loaded in memory")
        sys.exit(1)

    cfg = _wiki_cfg()
    wiki_base = _require_wiki_base(cfg)
    wiki_name = wiki_override or auto_wiki
    wiki_dir = _wiki_dir(wiki_name, wiki_base)
    ensure_wiki_structure(wiki_dir, wiki_name)

    task_name = getattr(task, "name", "")
    print(f"Task: {task_id} ({task_name})")
    print(f"Agent: {agent_name}")
    print(f"Wiki: {wiki_name}")
    if agent_cwd:
        print(f"Agent CWD: {agent_cwd}")

    # Check if already ingested
    ingested = load_ingested(wiki_dir)
    if task_id in ingested:
        prev_date = ingested[task_id]
        print(f"\nTask {task_id} already ingested on {prev_date}")
        if not force:
            print("Use --force to re-process.")
            return
        print("Force mode: re-processing.")

    # Delegate to the core ingest_task function
    result = await ingest_task(
        task, wiki_name,
        command=cfg["command"],
        wiki_base=wiki_base,
        timeout=cfg["timeout"],
        max_message_chars=cfg["max_message_chars"],
        retry_commands=cfg.get("retry_commands", ["glm", "ccs"]),
    )

    if not result:
        print("\nNo knowledge to ingest.")
        return

    updates = result.get("updates", 0)
    files = result.get("files", [])
    error = result.get("error")

    if error:
        print(f"\nError: {error}")
        return

    page_actions = result.get("page_actions", [])
    extract_attempts = result.get("extract_attempts", [])

    # Print step details
    for e in extract_attempts:
        print(f"  {e}")

    print(f"\nUpdates: {updates}")
    for pa in page_actions:
        print(f"  [{pa['action']}] {pa['file']}: {pa['title']}")

    if page_actions:
        print(f"\nFiles written: {len(files)}")
    else:
        print("\nNo pages updated.")
        return

    # Record in ingested.json
    ingested[task_id] = str(date.today())
    save_ingested(wiki_dir, ingested)
    print(f"Recorded task {task_id} in ingested.json")

    # Summary
    print("\n" + "=" * 50)
    print("Summary")
    print("=" * 50)
    print(f"Task: {task_id} ({task_name})")
    print(f"Wiki: {wiki_name}")
    extracted = result.get("extracted", True)
    print(f"Knowledge extracted: {'yes' if extracted else 'no'}")
    print(f"Wiki pages updated: {len(page_actions)}")
    for pa in page_actions:
        print(f"  [{pa['action']}] {pa['file']}")
    print(f"Files written: {len(files)}")

    await maybe_trigger_memforge_reindex()


# ── Batch mode ────────────────────────────────────────────────────────────────

async def run_batch(date: str | None = None) -> None:
    cfg = wiki_ingest_config()

    # Collect agents with wiki configured
    agents_with_wiki = []
    for aid in app_state.ordered_agent_ids():
        agent = app_state.get_agent(aid)
        if not agent:
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
    all_results: list[dict] = []

    for agent in agents_with_wiki:
        print(f"\n{'='*60}")
        print(f"Processing agent: {agent.name} (wiki: {agent.wiki})")
        print(f"{'='*60}")

        result = await ingest_agent_tasks(agent.id, target_date=date)

        all_results.append(result)

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

    await maybe_trigger_memforge_reindex()

    # Send Feishu notification
    feishu_cfg = cfg.get("feishu_notify", {})
    if feishu_cfg.get("enabled", False):
        from datetime import datetime
        from server.wiki_notify import send_wiki_digest

        digest_date = date or datetime.now().strftime("%Y-%m-%d")
        print(f"\nSending Feishu notification for {digest_date}...")
        success = await send_wiki_digest(feishu_cfg, all_results, digest_date)
        if success:
            print("Feishu notification sent.")
        else:
            print("Feishu notification failed (see logs for details).")
    else:
        print("\nFeishu notification disabled.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Wiki ingest: extract knowledge from tasks into wikis",
        epilog="""
Modes:
  Batch mode (default): process all agents with wiki configured
  Single task mode: process one task by session ID or task ID

Examples:
  python scripts/wiki_ingest.py
  python scripts/wiki_ingest.py --date 2026-04-14
  python scripts/wiki_ingest.py --task fbb4331d-c4b1-4e1e-a602-bb6123394cb4
  python scripts/wiki_ingest.py --task 54ed563070fd --force
  python scripts/wiki_ingest.py --task 54ed563070fd --wiki agent-park
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--task", type=str, default=None,
                      help="Single task mode: Session ID 或 Task ID")

    parser.add_argument("--date", type=str, default=None,
                        help="Batch mode: 只处理指定日期更新的任务 (YYYY-MM-DD)")
    parser.add_argument("--wiki", type=str, default=None,
                        help="Single task mode: 覆盖自动推导的 Wiki 名称")
    parser.add_argument("--force", action="store_true",
                        help="Single task mode: 强制重新处理已 ingest 的 task")

    args = parser.parse_args()

    if args.task:
        asyncio.run(run_single_task(args.task, force=args.force, wiki_override=args.wiki))
    else:
        asyncio.run(run_batch(date=args.date))


if __name__ == "__main__":
    main()
