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
            "- 每个页面有 YAML frontmatter（title, tags, sources, created, updated）\n"
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


async def extract_knowledge(conversation: str, command: str, timeout: int = 300) -> list[dict]:
    """First LLM call: extract knowledge points from conversation.

    Returns a list of knowledge point dicts, or empty list if nothing worth ingesting.
    """
    if not conversation.strip():
        return []

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

## 输出要求
如果没有值得沉淀的知识，输出空字符串。
如果有，输出 JSON 数组，每个元素：
{{
  "title": "知识点标题",
  "content": "详细内容（Markdown 格式）",
  "tags": ["建议的标签"],
  "category": "建议的分类"
}}"""

    result = await _llm_call(command, prompt, timeout=timeout)
    if not result:
        return []

    # Try to parse JSON from the result
    # The LLM may wrap the JSON in markdown code blocks
    json_str = result.strip()
    # Remove markdown code block wrapper if present
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
    if m:
        json_str = m.group(1).strip()

    try:
        items = json.loads(json_str)
        if isinstance(items, list):
            return items
        return []
    except json.JSONDecodeError:
        logger.warning("Failed to parse knowledge extraction result as JSON")
        return []


async def ingest_to_wiki(
    knowledge_points: list[dict],
    wiki_dir: Path,
    wiki_name: str,
    command: str,
    timeout: int = 300,
) -> dict:
    """Second LLM call: merge knowledge points into wiki.

    Returns the LLM's plan dict with updates and log_entry.
    """
    # Read current index
    index_path = wiki_dir / "index.md"
    index_content = ""
    if index_path.exists():
        index_content = index_path.read_text(encoding="utf-8")

    # Read existing page list for context
    pages_dir = wiki_dir / "pages"
    existing_pages = []
    if pages_dir.exists():
        for p in sorted(pages_dir.glob("*.md")):
            # Read title from frontmatter if possible
            title = p.stem
            try:
                text = p.read_text(encoding="utf-8")
                m = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
                if m:
                    title = m.group(1).strip()
            except Exception:
                pass
            existing_pages.append({"file": f"pages/{p.name}", "title": title})

    existing_pages_str = "\n".join(
        f"- {p['file']}: {p['title']}" for p in existing_pages
    ) if existing_pages else "（暂无页面）"

    knowledge_json = json.dumps(knowledge_points, ensure_ascii=False, indent=2)

    prompt = f"""你是一个 wiki 维护者。将以下知识点合并到 wiki 中。

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
4. 每个页面必须有 YAML frontmatter（title, tags, sources, created, updated）
5. 标签从已有标签中选择，确需新标签时说明
6. 更新 index.md：已有页面只更新摘要，新页面追加到合适分类
7. 如果已有页面需要更新，输出完整的页面 Markdown 内容（含更新后的 frontmatter）

## 输出 JSON
{{
  "updates": [
    {{
      "action": "update" | "create",
      "file": "pages/xxx.md",
      "title": "页面标题",
      "content": "完整的页面 Markdown 内容（含 frontmatter）",
      "summary": "一句话摘要（用于 index.md）",
      "tags": ["tag1", "tag2"],
      "category": "分类名称"
    }}
  ],
  "index_md": "完整的 index.md 内容（更新后）",
  "log_entry": "log.md 追加内容"
}}"""

    result = await _llm_call(command, prompt, timeout=timeout)
    if not result:
        return {"updates": [], "log_entry": ""}

    # Parse JSON from result
    json_str = result.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
    if m:
        json_str = m.group(1).strip()

    try:
        plan = json.loads(json_str)
        if isinstance(plan, dict):
            return plan
    except json.JSONDecodeError:
        logger.warning("Failed to parse wiki ingest plan as JSON")

    return {"updates": [], "log_entry": ""}


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
    wiki_base: str = "/data1/common/wiki",
    timeout: int = 300,
    max_message_chars: int = 50000,
) -> dict | None:
    """Ingest a single task's knowledge into the specified wiki.

    Returns {"updates": N, "files": [...]} or None if nothing to ingest.
    """
    wiki_dir = _wiki_dir(wiki_name, wiki_base)
    ensure_wiki_structure(wiki_dir, wiki_name)

    # Step 1: Extract conversation
    conversation = extract_conversation_summary(task, max_message_chars)
    if not conversation.strip():
        return None

    # Step 2: Extract knowledge (LLM call 1)
    knowledge_points = await extract_knowledge(conversation, command, timeout)
    if not knowledge_points:
        return None

    # Step 3: Ingest into wiki (LLM call 2)
    plan = await ingest_to_wiki(knowledge_points, wiki_dir, wiki_name, command, timeout)

    # Step 4: Apply updates
    files = apply_wiki_updates(wiki_dir, plan)

    # Step 5: Record in ingested.json
    ingested = load_ingested(wiki_dir)
    task_id = getattr(task, "id", "unknown")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ingested[task_id] = today
    save_ingested(wiki_dir, ingested)

    return {
        "updates": len(plan.get("updates", [])),
        "files": files,
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

    agent = app_state.get_agent(agent_id)
    if not agent:
        return {"error": f"Agent {agent_id} not found"}

    wiki_name = agent.wiki
    if not wiki_name:
        return {"tasks_processed": 0, "tasks_skipped": 0, "results": [], "reason": "no wiki configured"}

    wiki_dir = _wiki_dir(wiki_name, wiki_base)
    ensure_wiki_structure(wiki_dir, wiki_name)

    # Load ingested tasks
    ingested = load_ingested(wiki_dir)

    # Collect eligible tasks
    eligible_tasks = []
    skipped = 0
    for tid in agent.task_ids:
        task = app_state.get_task(tid)
        if not task:
            continue
        status = str(getattr(task, "status", ""))
        if status != "success":
            skipped += 1
            continue
        # Skip already ingested
        if tid in ingested:
            skipped += 1
            continue
        # Filter by date if specified
        if target_date:
            updated_at = getattr(task, "updated_at", "")
            if not updated_at.startswith(target_date):
                skipped += 1
                continue
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
                task, wiki_name, command, wiki_base, timeout, max_message_chars
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

    return {
        "wiki": wiki_name,
        "tasks_processed": len(results),
        "tasks_skipped": skipped,
        "results": results,
    }
