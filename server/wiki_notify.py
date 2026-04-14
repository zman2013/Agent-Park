"""Send wiki digest notifications via Feishu Bot CLI.

Two notification modes:
  - **Existing wiki** (has pages before this ingest): send a change summary card.
  - **New wiki** (created during this ingest): send one card per new page with its content.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Markdown helpers ───────────────────────────────────────────────────────────

def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter from a markdown string."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].strip()
    return text


def _truncate(text: str, max_chars: int = 3000) -> str:
    """Truncate text to max_chars, appending ellipsis if needed."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 3] + "..."


# ── Message formatters ─────────────────────────────────────────────────────────

def format_update_summary(results: list[dict], date: str) -> str:
    """Format a change-summary card for wiki ingest digest.

    Groups tasks by agent (not wiki), lists all date-matching tasks with status,
    and attaches page summaries inline under each task.
    """
    lines = [f"📋 wiki digest - {date}\n"]

    total_processed = 0
    total_skipped_incomplete = 0
    total_skipped_already_ingested = 0
    total_errors = 0
    total_extract_success = 0
    total_extract_all_failed = 0
    total_extract_retry_used = 0
    total_date_tasks = 0  # all tasks matching the date (including ineligible)

    # ── Per-agent grouped display ─────────────────────────────────────────
    for result in results:
        agent_id = result.get("agent_id", "")
        agent_name = result.get("agent_name", agent_id)

        if "error" in result:
            total_errors += 1
            lines.append(f"**{agent_name}** ❌ {result['error']}\n")
            continue

        if result.get("reason"):
            lines.append(f"**{agent_name}** — {result['reason']}\n")
            continue

        all_date_tasks = result.get("all_date_tasks", [])
        task_results = result.get("results", [])
        page_actions = result.get("page_actions", [])

        # Build a lookup: task_id -> result entry
        result_map = {}
        for r in task_results:
            result_map[r.get("task_id", "")] = r

        # Count total date-matching tasks for this agent
        n_date_tasks = len(all_date_tasks)
        if n_date_tasks == 0:
            # Skip agents with no date-matching tasks at all
            continue

        total_date_tasks += n_date_tasks

        # Aggregate per-agent stats
        processed = result.get("tasks_processed", 0)
        total_processed += processed
        total_skipped_incomplete += result.get("tasks_skipped_incomplete", 0)
        total_skipped_already_ingested += result.get("tasks_skipped_already_ingested", 0)

        stats = result.get("stats", {})
        total_extract_success += stats.get("extract_success", 0)
        total_extract_all_failed += stats.get("extract_all_failed", 0)
        total_extract_retry_used += stats.get("extract_retry_used", 0)

        lines.append(f"**{agent_name}**（{n_date_tasks} 个任务）")

        for dt in all_date_tasks:
            tid = dt.get("task_id", "")
            name = dt.get("task_name", tid or "?")
            r = result_map.get(tid)

            if r is None:
                if dt.get("already_ingested"):
                    lines.append(f"  - {name}: 已提取过")
                else:
                    status = dt.get("status", "unknown")
                    lines.append(f"  - {name}: {status}")
                continue

            # This task was actually processed
            error = r.get("error")
            updates = r.get("updates", 0)
            files = r.get("files", [])
            if error:
                total_errors += 1
                lines.append(f"  - {name}: {error}")
            elif updates > 0:
                file_names = [Path(f).stem for f in files]
                lines.append(f"  - {name}: {updates} 项更新（{', '.join(file_names)}）")
            else:
                lines.append(f"  - {name}: 未提取到有用知识")

            # Inline page summaries for this task
            task_pages = r.get("page_actions", [])
            for p in task_pages:
                title = p.get("title", Path(p.get("file", "")).stem)
                summary = p.get("summary", "")
                line = f"    + {title}"
                if summary:
                    line += f" — {summary}"
                lines.append(line)

        lines.append("")  # blank line between agents

    # ── Statistics ──────────────────────────────────────────────────────
    extract_total = total_extract_success + total_extract_all_failed
    if extract_total > 0:
        lines.append(
            f"提取统计：共提取 {extract_total} 个任务，提取到知识 {total_extract_success}，"
            f"未提取到知识 {total_extract_all_failed}，重试 {total_extract_retry_used} 次"
        )
        lines.append("")

    # Summary line
    date_scope = total_date_tasks
    reason_parts = []
    if total_skipped_incomplete:
        reason_parts.append(f"{total_skipped_incomplete} 个未启动状态")
    if total_skipped_already_ingested:
        reason_parts.append(f"{total_skipped_already_ingested} 个已提取过")
    reason_str = "，".join(reason_parts) if reason_parts else "0"
    lines.append(
        f"汇总：本轮扫描 {date_scope} 个任务（{date} 范围），处理 {total_processed} 个"
        f"（提取成功 {total_extract_success}，未提取到知识 {total_extract_all_failed}），"
        f"跳过 {date_scope - total_processed} 个（{reason_str}）"
    )

    return "\n".join(lines)


def format_new_wiki_pages(wiki_name: str, page_actions: list[dict], date: str) -> list[str]:
    """Format one card per new page for a brand-new wiki.

    Returns a list of card messages, one per created page.
    """
    cards: list[str] = []
    created_pages = [p for p in page_actions if p.get("action") == "create" and p.get("content")]

    for page in created_pages:
        title = page.get("title", Path(page.get("file", "")).stem)
        category = page.get("category", "")
        content = _strip_frontmatter(page.get("content", ""))
        content = _truncate(content)

        card_lines = [
            f"**📖 New Wiki Page — {wiki_name}**\n",
            f"**{title}**",
        ]
        if category:
            card_lines.append(f"分类: {category}")
        card_lines.append("")  # blank line
        card_lines.append(content)
        card_lines.append(f"\n---\n_{date}_")

        cards.append("\n".join(card_lines))

    return cards


def _wiki_is_new(result: dict) -> bool:
    """Determine if this wiki was newly created during this ingest.

    Uses the is_new_wiki flag set by ingest_agent_tasks based on whether
    the wiki had existing pages before the ingest run.
    """
    return result.get("is_new_wiki", False)


def _has_creatable_new_pages(result: dict) -> bool:
    """Return True when a new wiki result can produce at least one page card."""
    for page in result.get("page_actions", []):
        if page.get("action") == "create" and page.get("content"):
            return True
    return False


# ── Sending ────────────────────────────────────────────────────────────────────

async def send_feishu_card(feishu_cfg: dict, message: str) -> bool:
    """Send a Feishu card message using the feishu-bot CLI.

    Args:
        feishu_cfg: Configuration dict with keys: cli_path, chat_id, env_file.
        message: Markdown message content.

    Returns:
        True if sent successfully, False otherwise.
    """
    cli_path = feishu_cfg.get("cli_path", "")
    chat_id = feishu_cfg.get("chat_id", "")
    env_file = feishu_cfg.get("env_file", "")

    if not cli_path or not Path(cli_path).exists():
        logger.warning("Feishu CLI not found at %s, skipping notification", cli_path)
        return False

    if not chat_id:
        logger.warning("Feishu chat_id not configured, skipping notification")
        return False

    cmd = [
        "python3", cli_path,
        "send",
        "--chat-id", chat_id,
        "--card",
        "--max-len", "3500",
        "--quiet",
    ]
    if env_file:
        cmd.extend(["--env-file", env_file])
    cmd.append(message)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            logger.info("Feishu notification sent successfully")
            return True
        else:
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            logger.error("Feishu notification failed: %s", err_msg)
            return False
    except asyncio.TimeoutError:
        logger.error("Feishu notification timed out")
        return False
    except Exception:
        logger.exception("Feishu notification error")
        return False


async def send_wiki_digest(feishu_cfg: dict, results: list[dict], date: str) -> bool:
    """Format and send wiki digest notification via Feishu.

    For existing wikis: sends one summary card listing all changes.
    For new wikis: sends one card per new page with its content.

    Args:
        feishu_cfg: Feishu notification config (enabled, cli_path, chat_id, env_file).
        results: List of ingest results from wiki_ingest.
        date: Date string for the digest.

    Returns:
        True if all notifications were sent (or disabled), False on any failure.
    """
    if not feishu_cfg.get("enabled", False):
        logger.info("Feishu notification disabled, skipping")
        return True

    all_ok = True

    # All results go through the unified summary
    summary_msg = format_update_summary(results, date)
    all_ok = await send_feishu_card(feishu_cfg, summary_msg)
    if not all_ok:
        return False

    # Additionally, send per-page cards for new wikis with content
    for result in results:
        if "error" in result or result.get("reason"):
            continue
        if not _wiki_is_new(result) or not _has_creatable_new_pages(result):
            continue
        wiki_name = result.get("wiki", "unknown")
        page_actions = result.get("page_actions", [])
        cards = format_new_wiki_pages(wiki_name, page_actions, date)
        for card_msg in cards:
            ok = await send_feishu_card(feishu_cfg, card_msg)
            if not ok:
                all_ok = False

    return all_ok
