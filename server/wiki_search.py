"""Wiki knowledge pre-retrieval for agent prompts.

Two backends are supported:

* ``local``    — read ``{wiki}/index.md`` and let an LLM pick relevant pages
                 (two-phase: candidate selection + per-page verification).
* ``memforge`` — query the memforge knowledge index through its unified
                 entrypoint script and use the returned sources.

``local`` is the default so existing deployments keep working with no extra
setup. ``memforge`` requires the memforge script to be available at
``wiki_search.memforge_script``; if it fails at runtime we fall back to
``local`` automatically so the agent never sees a degraded prompt.
"""
from __future__ import annotations

import asyncio
import logging
import re
import textwrap
from pathlib import Path

logger = logging.getLogger(__name__)


async def search_wiki(prompt: str, wiki_name: str) -> str:
    """Return a formatted <wiki-context> block, or '' on any failure."""
    from server.config import wiki_search_config

    cfg = wiki_search_config()
    backend = cfg.get("backend", "local")

    if backend == "memforge":
        try:
            result = await _search_memforge(prompt, wiki_name, cfg)
        except Exception:
            logger.exception(
                "[wiki-search] memforge backend failed, falling back to local (wiki=%s)",
                wiki_name,
            )
        else:
            # A successful memforge query with zero hits is a legitimate
            # "no match" — respect it instead of paying LLM cost to second-guess.
            if result:
                return result
            logger.info(
                "[wiki-search] memforge returned no sections (wiki=%s); "
                "not falling back to local because the query itself succeeded",
                wiki_name,
            )
            return ""

    return await _search_local(prompt, wiki_name, cfg)


# ── Local backend (LLM picks pages from index.md) ─────────────────────────────


async def _search_local(prompt: str, wiki_name: str, cfg: dict) -> str:
    from server.wiki_ingest import _llm_call, _parse_json_from_llm_output, _read_page_metadata
    from server.wiki_prompts import phase1_selection

    wiki_base_raw = cfg.get("wiki_base") or ""
    if not wiki_base_raw:
        logger.info(
            "[wiki-search] wiki_base not configured; skipping local search (wiki=%s)",
            wiki_name,
        )
        return ""
    wiki_base = Path(wiki_base_raw)
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
    verify_tasks = [
        _verify_page_relevance(filename, pages_dir, prompt, cfg, wiki_name)
        for filename in filenames[: cfg["max_pages"]]
    ]
    results = await asyncio.gather(*verify_tasks)
    verified: list[str] = []
    for filename, relevant in zip(filenames[: cfg["max_pages"]], results):
        if relevant:
            verified.append(filename)
        else:
            logger.info("[wiki-search] phase2 rejected: %s", filename)

    if not verified:
        logger.info("[wiki-search] all candidates rejected by phase2 (wiki=%s)", wiki_name)
        return ""

    logger.info("[wiki-search] phase2 verified pages: %s", verified)

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

    return _wrap_context(sections, wiki_name)


# ── Memforge backend (vector search via unified entrypoint) ───────────────────


async def _search_memforge(prompt: str, wiki_name: str, cfg: dict) -> str:
    from server.memforge_client import memforge_search

    script_path = cfg.get("memforge_script", "") or ""
    if not script_path:
        logger.info(
            "[wiki-search] wiki_search.memforge_script not configured; "
            "falling back to local (wiki=%s)",
            wiki_name,
        )
        return ""

    wiki_base_raw = cfg.get("wiki_base") or ""
    if not wiki_base_raw:
        logger.info(
            "[wiki-search] wiki_base not configured; cannot resolve page paths "
            "for memforge hits (wiki=%s)",
            wiki_name,
        )
        return ""

    top_k = int(cfg.get("top_k") or cfg.get("max_pages") or 5)
    timeout = float(cfg.get("timeout", 15))
    max_pages = int(cfg.get("max_pages", 5))

    # Query a bit more than we need so we still have enough after filtering
    # out other wikis and deduping pages.
    query_top_k = max(top_k, max_pages) * 2

    logger.info(
        "[wiki-search] memforge query (wiki=%s, script=%s, top_k=%d)\nprompt preview: %.200s",
        wiki_name, script_path, query_top_k, prompt,
    )

    response = await memforge_search(
        prompt,
        kind="wiki",
        top_k=query_top_k,
        timeout=timeout,
        script_path=script_path,
        extra_targets={"wiki": wiki_base_raw},
    )

    semantic = response.get("semantic") or []
    if not isinstance(semantic, list):
        return ""

    wiki_base = Path(wiki_base_raw)
    wiki_dir = wiki_base / wiki_name
    wiki_prefix = f"wiki/{wiki_name}/"

    seen: set[Path] = set()
    page_paths: list[Path] = []
    for hit in semantic:
        source = (hit or {}).get("source", "")
        if not isinstance(source, str) or not source.startswith(wiki_prefix):
            continue
        rel = source[len(wiki_prefix):]
        page_path = wiki_dir / rel
        if page_path in seen:
            continue
        if not page_path.exists():
            continue
        # Only surface real page files (skip index.md / WIKI.md noise).
        if page_path.suffix.lower() != ".md":
            continue
        if page_path.name in ("index.md", "WIKI.md", "log.md"):
            continue
        seen.add(page_path)
        page_paths.append(page_path)
        if len(page_paths) >= max_pages:
            break

    if not page_paths:
        logger.info(
            "[wiki-search] memforge returned 0 eligible pages for wiki=%s (semantic=%d)",
            wiki_name, len(semantic),
        )
        return ""

    logger.info(
        "[wiki-search] memforge selected pages: %s",
        [str(p.relative_to(wiki_dir.parent)) for p in page_paths],
    )

    sections: list[str] = []
    for page_path in page_paths:
        section = _format_page_section(page_path, wiki_dir)
        if section:
            sections.append(section)

    return _wrap_context(sections, wiki_name)


# ── Shared formatting helpers ─────────────────────────────────────────────────


def _wrap_context(sections: list[str], wiki_name: str) -> str:
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
