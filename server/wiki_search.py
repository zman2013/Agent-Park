"""Wiki knowledge pre-retrieval for agent prompts."""
from __future__ import annotations

import asyncio
import logging
import re
import textwrap
from pathlib import Path

logger = logging.getLogger(__name__)


async def search_wiki(prompt: str, wiki_name: str) -> str:
    """Search wiki for knowledge relevant to prompt.

    Returns a formatted <wiki-context> block, or empty string if nothing found
    or any error occurs (never raises).
    """
    from server.config import wiki_search_config
    from server.wiki_ingest import _llm_call, _parse_json_from_llm_output, _read_page_metadata

    cfg = wiki_search_config()
    wiki_base = Path(cfg["wiki_base"])
    wiki_dir = wiki_base / wiki_name
    index_path = wiki_dir / "index.md"

    if not index_path.exists():
        logger.debug("[wiki-search] index not found: %s", index_path)
        return ""

    try:
        index_content = index_path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("[wiki-search] failed to read index: %s", index_path)
        return ""

    # Build page metadata for richer selection context
    page_meta = _read_page_metadata(wiki_dir)
    page_details_parts: list[str] = []
    for p in page_meta:
        line = f"- {p['file']}: {p['title']}"
        if p["summary"]:
            line += f"\n  摘要: {p['summary']}"
        if p["overview"]:
            line += f"\n  概览: {p['overview'][:300]}"
        page_details_parts.append(line)
    page_details_str = "\n".join(page_details_parts) if page_details_parts else ""

    # Ask LLM which pages are relevant (phase 1)
    from server.wiki_prompts import phase1_selection, phase2_verification
    selection_prompt = phase1_selection(prompt, index_content, page_details_str, cfg["max_pages"])

    logger.info(
        "[wiki-search] querying LLM (wiki=%s, command=%s, timeout=%s)\nprompt preview: %.200s",
        wiki_name, cfg["command"], cfg["timeout"], prompt,
    )

    raw = await _llm_call(cfg["command"], selection_prompt, timeout=cfg["timeout"])
    if not raw:
        logger.warning("[wiki-search] LLM returned empty response (wiki=%s)", wiki_name)
        return ""

    logger.info("[wiki-search] LLM raw response: %s", raw)

    parsed = _parse_json_from_llm_output(raw)
    if not isinstance(parsed, list) or not parsed:
        logger.info("[wiki-search] no relevant pages selected (wiki=%s), parsed=%r", wiki_name, parsed)
        return ""

    filenames: list[str] = [f for f in parsed if isinstance(f, str)]
    if not filenames:
        logger.info("[wiki-search] empty filename list after filtering (wiki=%s)", wiki_name)
        return ""

    logger.info("[wiki-search] selected pages: %s", filenames)

    # Phase 2: verify each candidate page is actually relevant
    pages_dir = wiki_dir / "pages"
    verified: list[str] = []
    verify_tasks = [
        _verify_page_relevance(filename, pages_dir, prompt, cfg, wiki_name)
        for filename in filenames[: cfg["max_pages"]]
    ]
    results = await asyncio.gather(*verify_tasks)
    for filename, relevant in zip(filenames[: cfg["max_pages"]], results):
        if relevant:
            verified.append(filename)
        else:
            logger.info("[wiki-search] phase2 rejected: %s", filename)

    if not verified:
        logger.info("[wiki-search] all candidates rejected by phase2 (wiki=%s)", wiki_name)
        return ""

    logger.info("[wiki-search] phase2 verified pages: %s", verified)

    # Build context from each verified page
    sections: list[str] = []
    for filename in verified:
        # Normalize: accept "pages/foo.md" or just "foo.md"
        name = filename.replace("pages/", "").strip("/")
        page_path = pages_dir / name
        if not page_path.exists():
            logger.warning("[wiki-search] page not found: %s", page_path)
            continue
        section = _format_page_section(page_path, wiki_dir)
        if section:
            sections.append(section)
        else:
            logger.warning("[wiki-search] failed to format page: %s", page_path)

    if not sections:
        logger.info("[wiki-search] no sections built, returning empty (wiki=%s)", wiki_name)
        return ""

    body = "\n\n".join(sections)
    result = (
        f"<wiki-context>\n"
        f"以下是与任务相关的 {wiki_name} 知识库信息：\n\n"
        f"{body}\n\n"
        f"如需完整内容，请读取上述文件路径。\n"
        f"</wiki-context>"
    )
    logger.info(
        "[wiki-search] built wiki-context with %d page(s) (wiki=%s):\n%s",
        len(sections), wiki_name, result,
    )
    return result


async def _verify_page_relevance(
    filename: str, pages_dir: Path, prompt: str, cfg: dict, wiki_name: str
) -> bool:
    """Phase 2: verify a candidate page is actually relevant using its summary."""
    from server.wiki_ingest import _llm_call
    from server.wiki_prompts import phase2_verification

    name = filename.replace("pages/", "").strip("/")
    page_path = pages_dir / name
    if not page_path.exists():
        return False
    try:
        content = page_path.read_text(encoding="utf-8")
    except Exception:
        return False

    title = name
    summary = ""
    overview = ""
    m = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
    if m:
        title = m.group(1).strip()
    m = re.search(r'^summary:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
    if m:
        summary = m.group(1).strip()
    m = re.search(r'^overview:\s*\|?\s*\n((?:  .+\n)+)', content, re.MULTILINE)
    if m:
        overview = textwrap.dedent(m.group(1)).strip()

    page_desc = f"标题：{title}\n"
    if summary:
        page_desc += f"摘要：{summary}\n"
    if overview:
        page_desc += f"概览：{overview}\n"

    verify_prompt = phase2_verification(prompt, page_desc)
    raw = await _llm_call(cfg["command"], verify_prompt, timeout=cfg["timeout"])
    result = (raw or "").strip().upper().startswith("YES")
    logger.info("[wiki-search] phase2 verify %s => %s (raw: %.20s)", filename, result, raw or "")
    return result


def _format_page_section(page_path: Path, wiki_dir: Path) -> str:
    """Extract title/summary/overview from a wiki page and format as a section."""
    try:
        text = page_path.read_text(encoding="utf-8")
    except Exception:
        return ""

    title = page_path.stem
    summary = ""
    overview = ""

    m = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
    if m:
        title = m.group(1).strip()

    m = re.search(r'^summary:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
    if m:
        summary = m.group(1).strip()

    m = re.search(r'^overview:\s*\|?\s*\n((?:  .+\n)+)', text, re.MULTILINE)
    if m:
        overview = textwrap.dedent(m.group(1)).strip()

    rel_path = page_path.relative_to(wiki_dir.parent)
    parts = [f"## {title}"]
    if summary:
        parts.append(summary)
    if overview:
        parts.append(overview)
    parts.append(f"📄 详见：{wiki_dir.parent}/{rel_path}")

    return "\n".join(parts)
