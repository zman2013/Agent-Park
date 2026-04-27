"""Wiki ingest: extract knowledge from task conversations and merge into wiki.

Directory layout per wiki:
  {wiki_base}/{wiki_name}/
  ├── WIKI.md          ← schema (auto-created on init)
  ├── index.md         ← page index (auto-updated)
  ├── log.md           ← append-only change log
  ├── pages/           ← knowledge pages (markdown)
  └── ingested.json    ← {task_id: "YYYY-MM-DD"} dedup record
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import textwrap
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Config ─────────────────────────────────────────────────────────────────────

def _get_config() -> dict:
    from server.config import wiki_ingest_config
    return wiki_ingest_config()


# ── Message extraction ─────────────────────────────────────────────────────────

def extract_conversation_summary(task, max_message_chars: int = 50000) -> str:
    """Extract text messages from a task, truncated per-message and overall."""
    messages = getattr(task, "messages", [])
    parts: list[str] = []
    total = 0
    for msg in messages:
        role = getattr(msg, "role", "")
        msg_type = getattr(msg, "type", "")
        content = getattr(msg, "content", "")
        if msg_type != "text":
            continue
        label = "user" if role == "user" else "agent"
        chunk = content[:800]
        line = f"[{label}] {chunk}"
        if total + len(line) > max_message_chars:
            break
        parts.append(line)
        total += len(line)
    return "\n".join(parts)


# ── Wiki structure helpers ─────────────────────────────────────────────────────

def _wiki_dir(wiki_name: str, wiki_base: str) -> Path:
    base_path = Path(wiki_base).resolve()
    candidate = (base_path / wiki_name).resolve()
    try:
        candidate.relative_to(base_path)
    except ValueError as exc:
        raise ValueError(f"Invalid wiki_name '{wiki_name}': path escapes wiki_base") from exc
    return candidate


def _safe_wiki_path(base_dir: Path, unsafe_path: str) -> Path:
    """Resolve and validate a path so it always stays under base_dir."""
    base_resolved = base_dir.resolve()
    candidate = (base_resolved / unsafe_path).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"Invalid wiki file path '{unsafe_path}': path escapes wiki dir") from exc
    return candidate


def _ingested_path(wiki_dir: Path) -> Path:
    return wiki_dir / "ingested.json"


def load_ingested(wiki_dir: Path) -> dict:
    path = _ingested_path(wiki_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_ingested(wiki_dir: Path, data: dict) -> None:
    wiki_dir.mkdir(parents=True, exist_ok=True)
    path = _ingested_path(wiki_dir)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_wiki_structure(wiki_dir: Path, wiki_name: str) -> None:
    """Initialize wiki directory structure if it doesn't exist."""
    if wiki_dir.exists() and (wiki_dir / "index.md").exists():
        return
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "pages").mkdir(exist_ok=True)

    # WIKI.md — minimal schema
    wiki_md = wiki_dir / "WIKI.md"
    if not wiki_md.exists():
        wiki_md.write_text(
            f"# Wiki Schema — {wiki_name} 知识库维护规范\n\n"
            "本文件定义 wiki 的结构、约定和维护流程。\n\n"
            "## 1 架构\n\n"
            "```\n"
            f"{wiki_name}/\n"
            "├── WIKI.md          ← 本文件（schema）\n"
            "├── index.md         ← 内容索引\n"
            "├── log.md           ← 变更日志（append-only）\n"
            "├── pages/           ← 知识页面（markdown）\n"
            "└── ingested.json    ← 已处理任务去重记录\n"
            "```\n\n"
            "## 2 页面约定\n\n"
            "- 文件名使用 kebab-case\n"
            "- 每个页面有 YAML frontmatter（title, summary, overview, tags, sources, created, updated）\n"
            "- summary: 一句话摘要（≤50字），用于快速判断页面是否相关\n"
            "- overview: 结构化概览（200-500字），描述页面的覆盖范围和适用场景\n"
            "- LLM 维护 index.md 和 log.md\n\n",
            encoding="utf-8",
        )

    # index.md — empty index
    index_md = wiki_dir / "index.md"
    if not index_md.exists():
        index_md.write_text(
            f"# {wiki_name} Wiki — 索引\n\n"
            f"> 本索引按主题分类列出 wiki 中的所有页面。\n\n"
            "---\n\n"
            "*（暂无页面）*\n",
            encoding="utf-8",
        )

    # log.md — empty log
    log_md = wiki_dir / "log.md"
    if not log_md.exists():
        log_md.write_text(
            f"# {wiki_name} Wiki — 变更日志\n\n"
            "> Append-only 日志，记录 wiki 的每次变更。\n"
            "> 格式：`## [YYYY-MM-DD] <操作> | <摘要>`\n\n"
            "---\n\n",
            encoding="utf-8",
        )

    # ingested.json — empty dict
    if not _ingested_path(wiki_dir).exists():
        save_ingested(wiki_dir, {})


# ── LLM calls ──────────────────────────────────────────────────────────────────


def _fix_json_newlines(json_str: str) -> str:
    """Fix unescaped newlines inside JSON string values.

    LLMs often output real newlines inside JSON strings instead of \n.
    This function uses a state-machine approach: scan for string values
    (between quotes) and replace real newlines with literal \n.
    """
    result = []
    i = 0
    in_string = False
    while i < len(json_str):
        ch = json_str[i]
        if ch == '"':
            # A quote is escaped only when preceded by an odd number of
            # consecutive backslashes.
            backslash_count = 0
            j = i - 1
            while j >= 0 and json_str[j] == '\\':
                backslash_count += 1
                j -= 1

            if backslash_count % 2 == 1:
                result.append(ch)
                i += 1
                continue

            in_string = not in_string
            result.append(ch)
        elif ch == '\n' and in_string:
            result.append('\\n')
        else:
            result.append(ch)
        i += 1
    return "".join(result)


async def _llm_call(command: str, prompt: str, timeout: int = 300) -> str:
    """Call an LLM command with -p flag, return result text."""
    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        from server.memory import _parse_stream_json_result
        result = _parse_stream_json_result(stdout.decode("utf-8", errors="replace"))
        return result.strip() if result else ""
    except asyncio.TimeoutError:
        logger.warning("Wiki ingest LLM call timed out (command=%s)", command)
        return ""
    except Exception:
        logger.exception("Wiki ingest LLM call failed (command=%s)", command)
        return ""


RETRY_COMMANDS = ["glm", "ccs"]  # default fallback, overridden by config


async def _try_extract_knowledge_with_retry(
    conversation: str,
    primary_command: str,
    timeout: int,
    retry_commands: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Try extract_knowledge with retry fallback across different LLMs.

    First tries primary command, then retries with glm and ccs.
    Returns (knowledge_points, log_entries).
    """
    log_entries: list[str] = []
    prompt = f"""你是一个知识提取器。分析以下 agent 工作对话，提取值得长期沉淀的知识点。

提取标准：
- 技术方案设计与架构决策（为什么选择 A 而不是 B）
- Bug 根因分析（问题是什么、根因是什么、如何修复）
- 调试方法论（如何定位问题的步骤和思路）
- 系统行为发现（某个模块/API 的非显而易见行为）
- 实现模式（可复用的代码模式、工作流程）
- 硬件约束或限制（硬件特性带来的软件约束）

不要提取：
- 简单的命令执行记录（git commit, npm install 等）
- 纯粹的代码搬运（没有决策过程的复制粘贴）
- 临时性的环境问题（某次网络超时等）

## 对话内容
{conversation}

只输出 JSON 数组，不要输出其他内容。如果无值得沉淀的知识，输出 []。每个元素格式：
{{
  "title": "知识点标题",
  "content": "详细内容（Markdown 格式，用 \\n 换行，不要出现真实换行符）",
  "tags": ["建议的标签"],
  "category": "建议的分类"
}}

重要：content 字段必须为单行字符串，用 \\n 表示换行，不要出现真实换行符。"""

    retry_commands = retry_commands or RETRY_COMMANDS

    # Build command list: primary + retries, all use same timeout
    commands = [(primary_command, timeout)] + [(c, timeout) for c in retry_commands]

    for i, (command, to) in enumerate(commands):
        label = f"primary({command})" if i == 0 else f"retry{i}({command})"
        result = await _llm_call(command, prompt, timeout=to)

        if not result:
            log_entries.append(f"  {label}: empty/timeout")
            continue

        # Try to parse JSON — use robust extraction
        json_str = result.strip()

        # 1. Try to extract from markdown code block
        #    Use first ``` marker + last ``` marker to handle nested code fences
        m = re.search(r"```(?:json)?\s*\n", json_str)
        if m:
            start = m.end()
            # Find closing ``` by scanning from end of string (handles nested fences)
            end = json_str.rfind("```", start)
            # Check if found ``` is on its own line (proper closing fence)
            while end > start:
                # Check if line before ``` is empty or whitespace only
                before = json_str[start:end].rstrip()
                line_before = before.rsplit("\n", 1)[-1]
                if line_before.strip() == "" or not line_before.strip().startswith("```"):
                    break  # proper closing
                end = json_str.rfind("```", start, end)
            if end > start:
                json_str = json_str[start:end].strip()
            else:
                json_str = json_str[start:].strip()

        # 2. Fix unescaped newlines in string values
        json_str = _fix_json_newlines(json_str)

        # 3. Try to find JSON object or array (including prose prefix like
        #    "Here is the JSON: [...]")
        if json_str.startswith("["):
            m2 = re.search(r"\[[\s\S]*\]", json_str)
            candidate = m2.group(0) if m2 else json_str
        elif json_str.startswith("{"):
            m2 = re.search(r"\{[\s\S]*\}", json_str)
            candidate = m2.group(0) if m2 else json_str
        else:
            m2 = re.search(r"\[[\s\S]*\]", json_str)
            if not m2:
                m2 = re.search(r"\{[\s\S]*\}", json_str)
            candidate = m2.group(0) if m2 else json_str

        try:
            items = json.loads(candidate, strict=False)
            if isinstance(items, list):
                log_entries.append(f"  {label}: {len(items)} item(s)")
                return items, log_entries
            log_entries.append(f"  {label}: not a list")
        except json.JSONDecodeError as e:
            log_entries.append(f"  {label}: JSON parse failed (pos {e.pos}: {e.msg})")

    log_entries.append("  all attempts failed")
    return [], log_entries


async def extract_knowledge(conversation: str, command: str, timeout: int = 300, retry_commands: list[str] | None = None) -> list[dict]:
    """First LLM call: extract knowledge points from conversation with retry.

    Returns a list of knowledge point dicts, or empty list if nothing worth ingesting.
    """
    if not conversation.strip():
        return []

    items, log_entries = await _try_extract_knowledge_with_retry(
        conversation, command, timeout, retry_commands=retry_commands,
    )
    for entry in log_entries:
        logger.info("extract_knowledge: %s", entry)
    return items


async def enrich_knowledge_summaries(
    knowledge_points: list[dict],
    command: str,
    timeout: int = 300,
) -> list[dict]:
    """Add summary and overview to each knowledge point.

    Separate LLM call to keep extract_knowledge fast and reliable.
    Returns the enriched list (same objects with summary/overview added).
    """
    if not knowledge_points:
        return knowledge_points

    kp_json = json.dumps(knowledge_points, ensure_ascii=False, indent=2)

    prompt = f"""你是一个技术文档编辑。为以下每个知识点生成 summary 和 overview。

要求：
- summary：一句话摘要，≤50字，用于快速判断内容是否相关
- overview：结构化概览，200-500字，描述知识点的覆盖范围和适用场景

## 知识点
{kp_json}

只输出 JSON 数组，不要输出其他内容。数组长度必须与输入一致。每个元素格式：
{{
  "summary": "...",
  "overview": "..."
}}

重要：summary 和 overview 字段必须为单行字符串，用 \\n 表示换行，不要出现真实换行符。"""

    result = await _llm_call(command, prompt, timeout=timeout)
    if not result:
        logger.warning("Failed to generate summaries for knowledge points")
        return knowledge_points

    json_str = result.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
    if m:
        json_str = m.group(1).strip()
    else:
        m2 = re.search(r"\[.*\]", json_str, re.DOTALL)
        if m2:
            json_str = m2.group(0)

    items = _parse_json_from_llm_output(result)
    if isinstance(items, list) and len(items) == len(knowledge_points):
        for kp, summary in zip(knowledge_points, items):
            if isinstance(summary, dict):
                kp["summary"] = summary.get("summary", "")
                kp["overview"] = summary.get("overview", "")
        return knowledge_points
    if not isinstance(items, list):
        logger.warning("Failed to parse summary enrichment result as JSON")

    return knowledge_points


async def ingest_to_wiki(
    knowledge_points: list[dict],
    wiki_dir: Path,
    wiki_name: str,
    command: str,
    timeout: int = 300,
) -> dict:
    """Second LLM call: merge knowledge points into wiki.

    Split into two phases for reliability:
    1. Decision phase: lightweight JSON — which files to create/update + metadata
    2. Content generation phase: one LLM call per new/updated page
    """
    # Read current index
    index_path = wiki_dir / "index.md"
    index_content = ""
    if index_path.exists():
        index_content = index_path.read_text(encoding="utf-8")

    # Read existing page list for context (with L0/L1 info)
    pages_dir = wiki_dir / "pages"
    existing_pages = []
    if pages_dir.exists():
        for p in sorted(pages_dir.glob("*.md")):
            title = p.stem
            summary = ""
            overview = ""
            try:
                text = p.read_text(encoding="utf-8")
                m_title = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
                if m_title:
                    title = m_title.group(1).strip()
                m_summary = re.search(r'^summary:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
                if m_summary:
                    summary = m_summary.group(1).strip()
                m_overview = re.search(r'^overview:\s*\|?\s*\n((?:  .+\n)+)', text, re.MULTILINE)
                if m_overview:
                    overview = textwrap.dedent(m_overview.group(1)).strip()
            except Exception:
                pass
            existing_pages.append({
                "file": f"pages/{p.name}",
                "title": title,
                "summary": summary,
                "overview": overview,
            })

    if existing_pages:
        parts = []
        for p in existing_pages:
            line = f"- {p['file']}: {p['title']}"
            if p['summary']:
                line += f"\n  L0: {p['summary']}"
            if p['overview']:
                line += f"\n  L1: {p['overview'][:300]}"
            parts.append(line)
        existing_pages_str = "\n".join(parts)
    else:
        existing_pages_str = "（暂无页面）"

    knowledge_json = json.dumps(knowledge_points, ensure_ascii=False, indent=2)

    # ── Phase 1: Decision — lightweight plan (no page content) ──
    decision_prompt = f"""你是一个 JSON-only 输出引擎。将以下知识点合并到 wiki 中。**禁止输出任何解释性文字、表格、Markdown 格式。**
只输出合法的 JSON 对象，不要输出其他任何内容。

## 当前 wiki 页面索引
{index_content}

## 已有页面文件
{existing_pages_str}

## 待合并的知识点
{knowledge_json}

## 规则
1. 优先合并到已有页面（如果主题匹配）
2. 只有全新主题才创建新页面
3. 文件名用 kebab-case
4. summary 是一句话摘要（≤50字）
5. overview 是结构化概览（200-500字）

输出格式：
{{
  "plan": [
    {{
      "action": "update" | "create",
      "file": "pages/xxx.md",
      "title": "页面标题",
      "summary": "一句话摘要",
      "overview": "结构化概览",
      "tags": ["tag1", "tag2"],
      "category": "分类名称"
    }}
  ],
  "log_entry": "log.md 追加内容"
}}

重要：所有字符串必须为单行，用 \\n 表示换行，不要出现真实换行符。"""

    result = await _llm_call(command, prompt=decision_prompt, timeout=timeout)
    if not result:
        return {"updates": [], "log_entry": ""}

    # Parse decision JSON
    plan_data = _parse_json_from_llm_output(result)
    if not plan_data or not isinstance(plan_data, dict):
        logger.warning("Failed to parse wiki ingest decision plan as JSON")
        return {"updates": [], "log_entry": ""}

    plan_items = plan_data.get("plan", [])
    log_entry = plan_data.get("log_entry", "")
    if not isinstance(plan_items, list):
        return {"updates": [], "log_entry": ""}

    # ── Phase 2: Generate content for each page ──
    updates = []
    for item in plan_items:
        filepath = item.get("file", "")
        action = item.get("action", "create")
        title = item.get("title", "")
        summary = item.get("summary", "")
        overview = item.get("overview", "")
        tags = item.get("tags", [])
        category = item.get("category", "")

        if not filepath or not title:
            continue

        # Read existing page content for update
        existing_content = ""
        if action == "update":
            try:
                existing_path = _safe_wiki_path(wiki_dir, filepath)
                if existing_path.exists():
                    existing_content = existing_path.read_text(encoding="utf-8")
            except Exception:
                pass

        # Generate page content
        content = await _generate_page_content(
            knowledge_points=knowledge_points,
            item=item,
            existing_content=existing_content,
            command=command,
            timeout=timeout,
        )

        if content:
            updates.append({
                "action": action,
                "file": filepath,
                "title": title,
                "summary": summary,
                "overview": overview,
                "content": content,
                "tags": tags,
                "category": category,
            })

    # ── Phase 3: Generate updated index.md ──
    index_md = await _generate_index(
        wiki_dir=wiki_dir,
        wiki_name=wiki_name,
        updates=updates,
        command=command,
        timeout=timeout,
    )

    return {"updates": updates, "index_md": index_md, "log_entry": log_entry}


def _parse_json_from_llm_output(text: str) -> dict | list | None:
    """Robust JSON extraction from LLM output."""
    json_str = text.strip()

    # 1. Try markdown code block — use the full match to include ``` fences
    m = re.search(r"```(?:json)?\s*\n?", json_str)
    if m:
        # Find the content after ```json... marker, then find the last ```
        start = m.end()
        end = json_str.rfind("```", start)
        if end > start:
            json_str = json_str[start:end].strip()
        else:
            json_str = json_str[start:].strip()

    # 2. Fix unescaped newlines in string values
    json_str = _fix_json_newlines(json_str)

    # 3. Try to find JSON object or array
    if json_str.startswith("{"):
        m2 = re.search(r"\{[\s\S]*\}", json_str)
        if m2:
            try:
                return json.loads(m2.group(0), strict=False)
            except json.JSONDecodeError:
                pass
    elif json_str.startswith("["):
        m2 = re.search(r"\[[\s\S]*\]", json_str)
        if m2:
            try:
                return json.loads(m2.group(0), strict=False)
            except json.JSONDecodeError:
                pass
    else:
        # Try both patterns
        m2 = re.search(r"\{[\s\S]*\}", json_str)
        if not m2:
            m2 = re.search(r"\[[\s\S]*\]", json_str)
        if m2:
            try:
                return json.loads(m2.group(0), strict=False)
            except json.JSONDecodeError:
                pass

    return None


async def _generate_page_content(
    knowledge_points: list[dict],
    item: dict,
    existing_content: str,
    command: str,
    timeout: int,
) -> str:
    """Generate full markdown content for a single wiki page."""
    action = item.get("action", "create")
    filepath = item.get("file", "")
    title = item.get("title", "")
    summary = item.get("summary", "")
    overview = item.get("overview", "")
    tags = item.get("tags", [])

    kp_json = json.dumps(knowledge_points, ensure_ascii=False, indent=2)

    if action == "update" and existing_content:
        prompt = f"""你是一个 wiki 页面维护者。更新已有页面，合并新知识点。

## 待合并的知识点
{kp_json}

## 当前页面内容
{existing_content}

## 要求
1. 保留已有内容中有价值的部分
2. 将相关知识点自然地融入页面
3. 更新 frontmatter 中的 summary 和 overview
4. 输出完整的更新后页面（不只是 diff）

只输出完整的页面 Markdown 内容，不要其他内容。**直接以 --- 开头（YAML frontmatter），不要有任何前导文字。**"""
    else:
        prompt = f"""你是一个 wiki 页面创建者。根据以下知识点创建一个新页面。

## 知识点
{kp_json}

## 页面元信息
- 标题: {title}
- 文件: {filepath}
- 摘要: {summary}
- 概览: {overview}
- 标签: {", ".join(tags)}

## 要求
1. 创建完整的 Markdown 页面
2. 必须有 YAML frontmatter（title, summary, overview, tags, sources, created, updated）
3. 正文内容要结构清晰

只输出页面 Markdown 内容，不要其他内容。以 --- 开头（YAML frontmatter）。"""

    result = await _llm_call(command, prompt=prompt, timeout=timeout)
    if not result:
        return ""

    # Strip leading conversational text — LLMs often add analysis before the actual content.
    # Find the first line starting with "---" (frontmatter) or "#" (heading).
    stripped = result.strip()
    for line in stripped.split("\n"):
        if line.startswith("---") or line.startswith("#"):
            stripped = stripped[stripped.index(line):]
            break
    else:
        # No valid markdown start found
        logger.warning("Page content for %s doesn't look like markdown", filepath)
        return ""

    if not stripped.startswith("---") and not stripped.startswith("#"):
        logger.warning("Page content for %s doesn't look like markdown", filepath)
        return ""

    return stripped


async def _generate_index(
    wiki_dir: Path,
    wiki_name: str,
    updates: list[dict],
    command: str,
    timeout: int,
) -> str:
    """Generate updated index.md content."""
    # Build list of all pages for context
    pages_dir = wiki_dir / "pages"
    page_list = []

    # Pages from updates (with their new summaries)
    for u in updates:
        page_list.append({
            "file": u["file"],
            "title": u.get("title", ""),
            "summary": u.get("summary", ""),
            "category": u.get("category", ""),
        })

    # Existing pages not in updates
    updated_files = {u["file"] for u in updates}
    if pages_dir.exists():
        for p in sorted(pages_dir.glob("*.md")):
            fpath = f"pages/{p.name}"
            if fpath not in updated_files:
                title = p.stem
                summary = ""
                try:
                    text = p.read_text(encoding="utf-8")
                    m_title = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
                    if m_title:
                        title = m_title.group(1).strip()
                    m_summary = re.search(r'^summary:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
                    if m_summary:
                        summary = m_summary.group(1).strip()
                except Exception:
                    pass
                page_list.append({
                    "file": fpath,
                    "title": title,
                    "summary": summary,
                    "category": "",
                })

    if not page_list:
        return ""

    page_json = json.dumps(page_list, ensure_ascii=False, indent=2)

    prompt = f"""你是 wiki 索引维护者。根据以下页面列表生成 index.md。

## 页面列表
{page_json}

## 要求
1. 按分类组织页面
2. 每个条目包含：文件名、标题、一句话摘要
3. 输出完整的 index.md Markdown 内容

只输出 index.md 内容，不要其他内容。"""

    result = await _llm_call(command, prompt=prompt, timeout=timeout)
    if not result:
        return ""

    stripped = result.strip()
    if not stripped:
        return ""

    # LLM often wraps the index content in a ```markdown ... ``` code block
    # with explanatory text before/after. Extract the code block content.
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", stripped, re.DOTALL)
    if m:
        stripped = m.group(1).strip()

    # Validate: index.md must start with a Markdown heading (any level)
    if not re.match(r"^#{1,6}\s", stripped):
        logger.warning("Generated index.md does not start with a heading, discarding")
        return ""

    return stripped


# ── File writing ────────────────────────────────────────────────────────────────

def apply_wiki_updates(wiki_dir: Path, plan: dict) -> list[str]:
    """Apply the wiki update plan: write pages, update index, append log.

    Returns list of files written.
    """
    written: list[str] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pages_dir = wiki_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    updates = plan.get("updates", [])
    if not updates:
        return written

    for update in updates:
        action = update.get("action", "create")
        filepath = update.get("file", "")
        content = update.get("content", "")

        if not filepath or not content:
            continue

        # Skip if content doesn't look like proper page content (not starting with --- or #)
        stripped_content = content.strip()
        if not stripped_content.startswith("---") and not stripped_content.startswith("#"):
            # The LLM sometimes outputs a diff or description instead of full page content
            # For "update" actions, we need to skip these since we can't apply them
            if action == "update":
                existing_path = wiki_dir / filepath
                if existing_path.exists():
                    logger.info("Skipping update for %s: content is not a full page, keeping existing", filepath)
                    continue
                else:
                    # No existing page, can't create from a diff - skip
                    logger.info("Skipping create for %s: content is not a full page", filepath)
                    continue

        # Defensive backfill: ensure summary/overview in frontmatter from plan fields
        summary = update.get("summary", "")
        overview = update.get("overview", "")
        if summary and "summary:" not in content:
            content = content.replace("---\n", f'---\nsummary: "{summary}"\n', 1)
        if overview and "overview:" not in content:
            content = content.replace("---\n", f"---\noverview: |\n  {overview}\n", 1)

        # Ensure frontmatter has updated date
        if f"updated:" in content:
            content = re.sub(r"updated:.*$", f"updated: {today}", content, flags=re.MULTILINE)

        # Resolve and validate target path to prevent traversal/absolute-path writes
        try:
            full_path = _safe_wiki_path(wiki_dir, filepath)
        except ValueError:
            logger.warning("Skip unsafe wiki update path: %s", filepath)
            continue

        # For update action, check that the page exists
        if action == "update" and not full_path.exists():
            # Page doesn't exist yet, treat as create
            action = "create"

        # Ensure created date for new pages
        if action == "create" and "created:" not in content:
            # Insert created date into frontmatter
            content = content.replace("---\n", f"---\ncreated: {today}\n", 1)

        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        written.append(filepath)

    # Update index.md if provided
    index_md = plan.get("index_md", "")
    if index_md:
        (wiki_dir / "index.md").write_text(index_md, encoding="utf-8")
        written.append("index.md")

    # Append log entry
    log_entry = plan.get("log_entry", "")
    if log_entry:
        log_path = wiki_dir / "log.md"
        existing = ""
        if log_path.exists():
            existing = log_path.read_text(encoding="utf-8")
        # Append the log entry
        if not existing.rstrip().endswith("---"):
            log_path.write_text(existing.rstrip() + "\n\n" + log_entry + "\n", encoding="utf-8")
        else:
            log_path.write_text(existing.rstrip() + "\n" + log_entry + "\n", encoding="utf-8")
        written.append("log.md")

    return written


# ── Main entry point ───────────────────────────────────────────────────────────

async def ingest_task(
    task,
    wiki_name: str,
    command: str,
    wiki_base: str,
    timeout: int = 300,
    max_message_chars: int = 50000,
    retry_commands: list[str] | None = None,
) -> dict | None:
    """Ingest a single task's knowledge into the specified wiki.

    Returns {"updates": N, "files": [...], "extract_attempts": [...]} or None if nothing to ingest.
    """
    wiki_dir = _wiki_dir(wiki_name, wiki_base)
    ensure_wiki_structure(wiki_dir, wiki_name)

    # Step 1: Extract conversation
    conversation = extract_conversation_summary(task, max_message_chars)
    if not conversation.strip():
        return None

    # Step 2: Extract knowledge (LLM call 1 with retry)
    knowledge_points, extract_attempts = await _try_extract_knowledge_with_retry(
        conversation, command, timeout, retry_commands=retry_commands,
    )
    if not knowledge_points:
        return {"updates": 0, "files": [], "page_actions": [], "log_entry": "", "extract_attempts": extract_attempts, "extracted": False}

    # Step 2.5: Enrich with summaries (LLM call 2)
    knowledge_points = await enrich_knowledge_summaries(knowledge_points, command, timeout)

    # Step 3: Ingest into wiki (LLM call 3)
    plan = await ingest_to_wiki(knowledge_points, wiki_dir, wiki_name, command, timeout)

    # Step 4: Apply updates
    files = apply_wiki_updates(wiki_dir, plan)

    # Determine which pages are newly created vs updated
    page_actions: list[dict] = []
    for update in plan.get("updates", []):
        action = update.get("action", "create")
        filepath = update.get("file", "")
        title = update.get("title", filepath)
        summary = update.get("summary", "")
        category = update.get("category", "")
        content = update.get("content", "")
        page_actions.append({
            "action": action,
            "file": filepath,
            "title": title,
            "summary": summary,
            "category": category,
            "content": content,
        })

    # Step 5: Record in ingested.json
    ingested = load_ingested(wiki_dir)
    task_id = getattr(task, "id", "unknown")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ingested[task_id] = today
    save_ingested(wiki_dir, ingested)

    return {
        "updates": len(plan.get("updates", [])),
        "files": files,
        "page_actions": page_actions,
        "log_entry": plan.get("log_entry", ""),
        "extract_attempts": extract_attempts,
    }


async def ingest_agent_tasks(
    agent_id: str,
    target_date: str | None = None,
    progress_cb=None,
) -> dict:
    """Ingest all eligible tasks for an agent into its wiki.

    Returns {"tasks_processed": N, "tasks_skipped": N, "results": [...]}.
    """
    from server.config import wiki_ingest_config
    from server.state import app_state

    cfg = wiki_ingest_config()
    command = cfg["command"]
    wiki_base = cfg["wiki_base"]
    timeout = cfg["timeout"]
    max_message_chars = cfg["max_message_chars"]
    retry_commands = cfg.get("retry_commands", RETRY_COMMANDS)

    if not wiki_base:
        return {"error": "wiki_ingest.wiki_base is not configured in config.json"}

    agent = app_state.get_agent(agent_id)
    if not agent:
        return {"error": f"Agent {agent_id} not found"}

    wiki_name = agent.wiki
    if not wiki_name:
        return {"tasks_processed": 0, "tasks_skipped": 0, "results": [], "reason": "no wiki configured"}

    wiki_dir = _wiki_dir(wiki_name, wiki_base)

    # Check if wiki already has content pages before ingest
    pages_dir = wiki_dir / "pages"
    had_existing_pages = pages_dir.exists() and any(pages_dir.glob("*.md"))

    ensure_wiki_structure(wiki_dir, wiki_name)

    # Load ingested tasks
    ingested = load_ingested(wiki_dir)

    # Collect all date-matching tasks with their status for the digest
    all_date_tasks: list[dict] = []
    eligible_tasks = []
    skipped_incomplete = 0  # idle/running/waiting — not yet finished
    skipped_already_ingested = 0
    skipped_date_mismatch = 0
    for tid in agent.task_ids:
        task = app_state.get_task(tid)
        if not task:
            continue
        status = getattr(task, "status", None)
        if status is not None:
            status = status.value if hasattr(status, "value") else str(status)
        # Filter by date first so all counters are date-scoped
        updated_at = getattr(task, "updated_at", "")
        if target_date and not updated_at.startswith(target_date):
            skipped_date_mismatch += 1
            continue
        # Record task status for digest
        if status not in ("success", "failed"):
            skipped_incomplete += 1
            all_date_tasks.append({"task_id": tid, "task_name": getattr(task, "name", ""), "status": status})
            continue
        # Skip already ingested
        if tid in ingested:
            skipped_already_ingested += 1
            all_date_tasks.append({"task_id": tid, "task_name": getattr(task, "name", ""), "status": status, "already_ingested": True})
            continue
        all_date_tasks.append({"task_id": tid, "task_name": getattr(task, "name", ""), "status": status})
        eligible_tasks.append(task)

    results = []
    for i, task in enumerate(eligible_tasks):
        if progress_cb:
            await progress_cb(
                "wiki_ingest",
                f"[{i + 1}/{len(eligible_tasks)}] 处理任务: {getattr(task, 'name', task.id)}",
            )
        try:
            result = await ingest_task(
                task, wiki_name, command, wiki_base, timeout, max_message_chars, retry_commands
            )
            if result:
                results.append({
                    "task_id": task.id,
                    "task_name": getattr(task, "name", ""),
                    **result,
                })
        except Exception:
            logger.exception("Failed to ingest task %s", task.id)
            results.append({
                "task_id": task.id,
                "task_name": getattr(task, "name", ""),
                "error": "ingest failed",
            })

    # Aggregate page_actions from all task results
    all_page_actions: list[dict] = []
    for r in results:
        for pa in r.get("page_actions", []):
            all_page_actions.append(pa)

    # Aggregate statistics from extract_attempts
    extract_success = 0
    extract_all_failed = 0
    extract_retry_used = 0
    for r in results:
        attempts = r.get("extract_attempts", [])
        if attempts:
            extracted_flag = r.get("extracted", True)
            if extracted_flag:
                extract_success += 1
                if len(attempts) > 1:
                    extract_retry_used += 1
            else:
                extract_all_failed += 1
    extract_total = extract_success + extract_all_failed

    return {
        "wiki": wiki_name,
        "agent_id": agent_id,
        "agent_name": getattr(agent, "name", agent_id),
        "tasks_processed": len(results),
        "tasks_skipped_incomplete": skipped_incomplete,  # idle/running/waiting
        "tasks_skipped_already_ingested": skipped_already_ingested,
        "tasks_skipped_date_mismatch": skipped_date_mismatch,
        "all_date_tasks": all_date_tasks,
        "results": results,
        "page_actions": all_page_actions,
        "is_new_wiki": not had_existing_pages,
        "stats": {
            "extract_total": extract_total,
            "extract_success": extract_success,
            "extract_all_failed": extract_all_failed,
            "extract_retry_used": extract_retry_used,
        },
    }


# ── Memforge reindex hook ─────────────────────────────────────────────────────


async def maybe_trigger_memforge_reindex() -> None:
    """If configured, refresh the memforge index after a successful ingest.

    Shared entry point used by both the in-process scheduler
    (``server.routes_ws._run_daily_wiki_ingest``) and the CLI script
    (``scripts/wiki_ingest.py``). Any failure is logged but does not affect
    the caller's outcome.
    """
    from server.config import wiki_ingest_config
    from server.memforge_client import memforge_reindex, MemforgeError

    cfg = wiki_ingest_config()
    if not cfg.get("memforge_reindex_enabled"):
        return

    script = (cfg.get("memforge_reindex_script") or "").strip()
    if not script:
        logger.info(
            "[memforge] reindex enabled but 'memforge_reindex_script' is not "
            "configured; skipping."
        )
        return
    timeout = float(cfg.get("memforge_reindex_timeout", 600))

    extra_targets: dict[str, str] = {}
    wiki_base = (cfg.get("wiki_base") or "").strip()
    if wiki_base:
        extra_targets["wiki"] = wiki_base

    logger.info(
        "[memforge] triggering reindex (script=%s, timeout=%ss)", script, timeout,
    )
    try:
        rc = await memforge_reindex(
            kind="wiki", timeout=timeout, script_path=script, quiet=True,
            extra_targets=extra_targets or None,
        )
    except MemforgeError as exc:
        logger.warning("[memforge] reindex skipped: %s", exc)
        return
    if rc == 0:
        logger.info("[memforge] reindex completed.")
    else:
        logger.warning("[memforge] reindex exit=%s (see logs)", rc)
