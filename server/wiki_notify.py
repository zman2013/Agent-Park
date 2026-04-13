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
    """Format a change-summary card for existing wiki updates.

    Used when a wiki already had pages and only received updates.
    """
    lines = [f"**📋 Wiki Digest — {date}**\n"]

    total_processed = 0
    total_skipped = 0
    total_errors = 0
    # Aggregate stats across all wikis
    total_extract_success = 0
    total_extract_all_failed = 0
    total_extract_retry_used = 0

    for result in results:
        wiki = result.get("wiki", "unknown")
        processed = result.get("tasks_processed", 0)
        skipped = result.get("tasks_skipped", 0)
        total_processed += processed
        total_skipped += skipped

        # Collect per-agent stats
        stats = result.get("stats", {})
        total_extract_success += stats.get("extract_success", 0)
        total_extract_all_failed += stats.get("extract_all_failed", 0)
        total_extract_retry_used += stats.get("extract_retry_used", 0)

        if "error" in result:
            total_errors += 1
            lines.append(f"**{wiki}** ❌ {result['error']}\n")
            continue

        if result.get("reason"):
            lines.append(f"**{wiki}** — {result['reason']}\n")
            continue

        if processed == 0:
            lines.append(f"**{wiki}** — 无新任务\n")
            continue

        lines.append(f"**{wiki}** ({processed} task{'s' if processed != 1 else ''})")

        for r in result.get("results", []):
            name = r.get("task_name", r.get("task_id", "?"))
            updates = r.get("updates", 0)
            files = r.get("files", [])
            error = r.get("error")
            if error:
                total_errors += 1
                lines.append(f"  - ✗ {name}: {error}")
            elif updates > 0:
                file_names = [Path(f).stem for f in files]
                lines.append(f"  - ✓ {name}: {updates} update(s) ({', '.join(file_names)})")
            else:
                lines.append(f"  - ○ {name}: no knowledge extracted")

        # List created / updated pages with summaries
        page_actions = result.get("page_actions", [])
        if page_actions:
            created = [p for p in page_actions if p.get("action") == "create"]
            updated = [p for p in page_actions if p.get("action") == "update"]
            if created:
                lines.append("  新增页面:")
                for p in created:
                    title = p.get("title", Path(p.get("file", "")).stem)
                    summary = p.get("summary", "")
                    line = f"    + {title}"
                    if summary:
                        line += f" — {summary}"
                    lines.append(line)
            if updated:
                lines.append("  更新页面:")
                for p in updated:
                    title = p.get("title", Path(p.get("file", "")).stem)
                    summary = p.get("summary", "")
                    line = f"    ~ {title}"
                    if summary:
                        line += f" — {summary}"
                    lines.append(line)

        lines.append("")  # blank line between wikis

    # Stats line
    if total_extract_all_failed or total_extract_retry_used:
        total_extracted = total_extract_success + total_extract_all_failed
        lines.append(f"**提取统计**: {total_extracted} 个任务，成功 {total_extract_success}，失败 {total_extract_all_failed}，重试 {total_extract_retry_used} 次")
    lines.append("")  # blank line before summary

    lines.append(f"**汇总**: {total_processed} processed, {total_skipped} skipped, {total_errors} errors")

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

    # Separate results into new-wiki and existing-wiki groups
    existing_wiki_results: list[dict] = []
    new_wiki_results: list[dict] = []

    for result in results:
        if "error" in result or result.get("reason") or result.get("tasks_processed", 0) == 0:
            # Errors/skips/empty go into summary
            existing_wiki_results.append(result)
            continue

        # New-wiki results only go down the per-page path when they can
        # actually generate at least one page card. Otherwise they must be
        # summarized so failures are still surfaced to operators.
        if _wiki_is_new(result) and _has_creatable_new_pages(result):
            new_wiki_results.append(result)
        else:
            existing_wiki_results.append(result)

    # Send update summary for existing wikis (one card)
    if existing_wiki_results:
        summary_msg = format_update_summary(existing_wiki_results, date)
        ok = await send_feishu_card(feishu_cfg, summary_msg)
        if not ok:
            all_ok = False

    # Send one card per new page for new wikis
    for result in new_wiki_results:
        wiki_name = result.get("wiki", "unknown")
        page_actions = result.get("page_actions", [])
        cards = format_new_wiki_pages(wiki_name, page_actions, date)
        for card_msg in cards:
            ok = await send_feishu_card(feishu_cfg, card_msg)
            if not ok:
                all_ok = False

    return all_ok
