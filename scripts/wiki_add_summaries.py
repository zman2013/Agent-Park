#!/usr/bin/env python3
"""Migration script: generate L0 summary and L1 overview for existing wiki pages.

Scans all wiki pages under the configured wiki base, finds those missing
summary or overview in frontmatter, calls LLM to generate them, and
writes back to the file.

Usage:
    cd /data1/common/agent-park
    python scripts/wiki_add_summaries.py [--wiki-dir /path/to/wiki]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

# Add project root to sys.path so server.* imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.wiki_ingest import _llm_call


def read_frontmatter(text: str) -> tuple[dict, int, int]:
    """Parse YAML frontmatter from markdown text.

    Returns (fields_dict, start_line, end_line).
    start_line is the index of the opening ---, end_line is the closing ---.
    If no frontmatter found, returns ({}, -1, -1).
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, -1, -1

    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_lines = lines[1:i]
            fields: dict = {}
            for line in fm_lines:
                m = re.match(r"^(\w[\w_]*):\s*(.+)$", line)
                if m:
                    key = m.group(1)
                    value = m.group(2).strip()
                    # Remove quotes
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    fields[key] = value
            return fields, 0, i

    return {}, -1, -1


def build_summary_prompt(page_content: str) -> str:
    return f"""你是一个技术文档编辑。为以下 wiki 页面生成 summary 和 overview。

要求：
- summary：一句话摘要，≤50字，用于快速判断页面是否相关
- overview：结构化概览，200-500字，描述页面的覆盖范围和适用场景

## 页面内容
{page_content}

只输出 JSON，不要输出其他内容。格式：
{{
  "summary": "...",
  "overview": "..."
}}"""


async def generate_summary(content: str, command: str, timeout: int = 300) -> dict:
    result = await _llm_call(command, build_summary_prompt(content), timeout=timeout)
    if not result:
        return {}

    json_str = result.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
    if m:
        json_str = m.group(1).strip()
    else:
        m2 = re.search(r"\{.*\}", json_str, re.DOTALL)
        if m2:
            json_str = m2.group(0)

    try:
        data = json.loads(json_str, strict=False)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    return {}


def inject_frontmatter_fields(text: str, summary: str, overview: str) -> str:
    """Insert summary and overview into existing frontmatter."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text

    closing_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing_idx = i
            break

    if closing_idx == -1:
        return text

    insert_lines: list[str] = []
    if summary:
        insert_lines.append(f'summary: "{summary}"')
    if overview:
        insert_lines.append("overview: |")
        for ol in overview.split("\n"):
            insert_lines.append(f"  {ol}")

    if insert_lines:
        lines[closing_idx:closing_idx] = insert_lines

    return "\n".join(lines)


def _get_config() -> dict:
    from server.config import wiki_ingest_config
    return wiki_ingest_config()


async def main(wiki_dir: str, command: str, dry_run: bool, timeout: int) -> None:
    wiki_path = Path(wiki_dir)
    if not wiki_path.exists():
        print(f"Error: wiki directory '{wiki_dir}' does not exist.")
        sys.exit(1)

    pages_dir = wiki_path / "pages"
    if not pages_dir.exists():
        print(f"No pages directory found under '{wiki_dir}'.")
        return

    pages = sorted(pages_dir.glob("*.md"))
    if not pages:
        print("No wiki pages found.")
        return

    print(f"Scanning {len(pages)} wiki page(s) for missing summary/overview...")

    missing = []
    for p in pages:
        text = p.read_text(encoding="utf-8")
        fm, _, _ = read_frontmatter(text)
        has_summary = bool(fm.get("summary"))
        has_overview = bool(fm.get("overview"))
        if not has_summary or not has_overview:
            missing.append((p, text, has_summary, has_overview))

    if not missing:
        print("All pages already have summary and overview. Nothing to do.")
        return

    print(f"\n{len(missing)} page(s) need summary/overview generation:")
    for p, _, has_s, has_o in missing:
        needs = []
        if not has_s:
            needs.append("summary")
        if not has_o:
            needs.append("overview")
        print(f"  - {p.name} ({', '.join(needs)})")

    if dry_run:
        print("\nDry run mode — no changes will be made.")
        return

    print("\nGenerating summaries via LLM...")
    success_count = 0
    error_count = 0

    for page_path, text, has_summary, has_overview in missing:
        print(f"\n  Processing: {page_path.name}")

        new_summary = "" if not has_summary else None
        new_overview = "" if not has_overview else None

        if new_summary is not None or new_overview is not None:
            result = await generate_summary(text, command, timeout=timeout)
            s = result.get("summary", "")
            o = result.get("overview", "")

            if s:
                print(f"    summary: {s[:60]}...")
            else:
                print(f"    summary: (failed to generate)")

            if o:
                print(f"    overview: {o[:60]}...")
            else:
                print(f"    overview: (failed to generate)")

            if s or o:
                new_text = inject_frontmatter_fields(text, s, o)
                if not dry_run:
                    page_path.write_text(new_text, encoding="utf-8")
                success_count += 1
            else:
                error_count += 1

    print(f"\nDone: {success_count} page(s) updated, {error_count} failed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate L0 summary and L1 overview for existing wiki pages."
    )
    parser.add_argument(
        "--wiki-dir",
        type=str,
        default=None,
        help="Path to wiki root directory (default: wiki_ingest.wiki_base from config.json)",
    )
    parser.add_argument(
        "--command",
        type=str,
        default=None,
        help="LLM command override (default: from config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only scan and report, don't modify files",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="LLM call timeout in seconds (default: 300)",
    )
    args = parser.parse_args()

    cfg = _get_config()
    command = args.command or cfg.get("command", "claude")

    wiki_dir = args.wiki_dir or (cfg.get("wiki_base") or "").strip()
    if not wiki_dir:
        print(
            "Error: wiki directory is not configured. Pass --wiki-dir or set "
            "wiki_ingest.wiki_base in config.json.",
            file=sys.stderr,
        )
        sys.exit(2)

    asyncio.run(
        main(
            wiki_dir=wiki_dir,
            command=command,
            dry_run=args.dry_run,
            timeout=args.timeout,
        )
    )
