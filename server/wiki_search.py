"""Wiki knowledge pre-retrieval for agent prompts."""
from __future__ import annotations

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
    from server.wiki_ingest import _llm_call, _parse_json_from_llm_output

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

    # Ask LLM which pages are relevant
    selection_prompt = (
        f"你是 wiki 知识检索引擎。根据用户任务，从索引中选出相关页面。\n\n"
        f"用户任务：{prompt}\n\n"
        f"Wiki 索引：\n{index_content}\n\n"
        f"输出 JSON 数组，包含相关页面的文件名（如 \"pages/xxx.md\"，最多 {cfg['max_pages']} 个）。"
        f"无相关页面则输出空数组 []。只输出 JSON，不要其他内容。"
    )

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

    # Build context from each matched page's frontmatter
    sections: list[str] = []
    pages_dir = wiki_dir / "pages"
    for filename in filenames[: cfg["max_pages"]]:
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
