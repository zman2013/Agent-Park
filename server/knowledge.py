"""Knowledge summary: extract reusable knowledge from task history.

Directory layout:
  data/knowledge/{effective_id}/
  ├── errors.md       # error experience
  ├── project.md      # project knowledge
  └── hotfiles.md     # file heat statistics
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KNOWLEDGE_DIR = DATA_DIR / "knowledge"


# ── Directory helpers ──────────────────────────────────────────────────────────

def effective_knowledge_agent_id(agent_id: str) -> str:
    """Return the agent id whose knowledge dir should be used (same logic as memory)."""
    from server.state import app_state
    agent = app_state.get_agent(agent_id)
    if agent and agent.shared_memory_agent_id:
        return agent.shared_memory_agent_id
    return agent_id


def knowledge_dir(agent_id: str) -> Path:
    eid = effective_knowledge_agent_id(agent_id)
    return KNOWLEDGE_DIR / eid


# ── Signal extraction (no LLM) ────────────────────────────────────────────────

_USER_CORRECTION_KEYWORDS = (
    "不对", "错了", "不是", "应该", "正确", "改成", "你弄错", "不要", "不能",
    "wrong", "incorrect", "should be", "mistake", "fix", "不应该",
)


def extract_error_signals(tasks: list) -> list[dict]:
    """Extract error-related message fragments from tasks."""
    signals = []
    for task in tasks:
        task_signals = []
        messages = task.messages if hasattr(task, "messages") else []
        for msg in messages:
            role = getattr(msg, "role", "")
            msg_type = getattr(msg, "type", "")
            content = getattr(msg, "content", "")
            tool_name = getattr(msg, "tool_name", "")

            # tool execution failure (tool_result with error keywords)
            if role == "agent" and msg_type == "tool_result":
                low = content.lower()
                if any(kw in low for kw in ["error", "traceback", "failed", "exception", "errno"]):
                    task_signals.append({
                        "source": "tool_error",
                        "tool": tool_name,
                        "content": content[:800],
                        "task_id": task.id,
                        "task_name": task.name,
                    })

            # user correction: only short messages with correction keywords (not arbitrary long texts)
            if role == "user" and msg_type == "text":
                has_correction = any(kw in content for kw in _USER_CORRECTION_KEYWORDS)
                # Short messages are more likely corrections; long ones are new tasks/context
                is_short = len(content) < 200
                if has_correction or (is_short and len(content) > 5):
                    task_signals.append({
                        "source": "user_correction",
                        "content": content[:400],
                        "task_id": task.id,
                        "task_name": task.name,
                    })

        # task failed: keep all signals from that task
        status = getattr(task, "status", "")
        if str(status) == "failed":
            for s in task_signals:
                s["task_failed"] = True

        # high turns (> 20): mark as potential detour
        num_turns = getattr(task, "num_turns", 0)
        if num_turns > 20:
            for s in task_signals:
                s["high_turns"] = True

        signals.extend(task_signals)
    return signals


_AGENT_PARK_NOISE_KEYWORDS = (
    "knowledge.py",
    "knowledge_summary",
    "merge_errors",
    "merge_project",
    "extract_error_signals",
    "extract_project_signals",
    "generate_summary",
    "知识总结",
    "knowledge summary",
    "build_memory_entries",
    "update_memory_index",
)


def extract_project_signals(tasks: list) -> list[dict]:
    """Extract project knowledge fragments from agent text messages."""
    signals = []
    for task in tasks:
        messages = task.messages if hasattr(task, "messages") else []
        # Only keep the first agent text per task to avoid repetition
        agent_texts_seen = 0
        for msg in messages:
            role = getattr(msg, "role", "")
            msg_type = getattr(msg, "type", "")
            content = getattr(msg, "content", "")

            # agent text describing project structure / commands
            if role == "agent" and msg_type == "text" and len(content) > 30:
                # Skip content that is about agent-park internals (self-referential noise)
                if any(kw in content for kw in _AGENT_PARK_NOISE_KEYWORDS):
                    continue
                # heuristic: contains path separators or command keywords
                if any(kw in content for kw in ["/", "python ", "pytest", "目录", "文件", "路径", "命令", "command", "script"]):
                    if agent_texts_seen < 3:  # limit agent texts per task
                        signals.append({
                            "source": "agent_text",
                            "content": content[:800],
                            "task_id": task.id,
                            "task_name": task.name,
                        })
                        agent_texts_seen += 1

            # user providing facts
            if role == "user" and msg_type == "text" and len(content) > 10:
                # Skip agent-park internal discussions
                if any(kw in content for kw in _AGENT_PARK_NOISE_KEYWORDS):
                    continue
                signals.append({
                    "source": "user_text",
                    "content": content[:600],
                    "task_id": task.id,
                    "task_name": task.name,
                })
    return signals


_HOTFILE_EXCLUDE_PREFIXES = (
    "/tmp/",
    "/var/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/run/",
)


def _is_under_root(fp: str, root: str) -> bool:
    """Return True if fp equals root or is nested under root (prefix-safe)."""
    root_clean = root.rstrip("/")
    return fp == root_clean or fp.startswith(f"{root_clean}/")


def _is_project_file(fp: str, project_roots: tuple[str, ...] = ()) -> bool:
    """Return True if the file path is a meaningful project file (not temp/system noise)."""
    fp = fp.strip()
    if not fp:
        return False

    for prefix in _HOTFILE_EXCLUDE_PREFIXES:
        if fp.startswith(prefix):
            # Keep files inside known project roots even if repo is under /tmp, /var, etc.
            if project_roots and any(_is_under_root(fp, root) for root in project_roots):
                return True
            return False
    return True


def compute_hotfiles(tasks: list, recent_days: int = 7, project_root: str | None = None) -> list[dict]:
    """Count file access frequency from tool_use messages (no LLM)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days)
    read_counts: dict[str, int] = defaultdict(int)
    edit_counts: dict[str, int] = defaultdict(int)
    last_access: dict[str, str] = {}
    project_roots = tuple([project_root.rstrip("/")]) if project_root else ()

    for task in tasks:
        # check task updated_at for recency
        updated_at = getattr(task, "updated_at", "")
        try:
            ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if ts < cutoff:
                continue
        except Exception:
            pass

        messages = task.messages if hasattr(task, "messages") else []
        for msg in messages:
            role = getattr(msg, "role", "")
            msg_type = getattr(msg, "type", "")
            tool_name = getattr(msg, "tool_name", "")
            content = getattr(msg, "content", "")

            if role != "agent" or msg_type != "tool_use":
                continue

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            if tool_name in ("Read",):
                # content is JSON with file_path
                fp = _extract_file_path(content, tool_name)
                if fp and _is_project_file(fp, project_roots):
                    read_counts[fp] += 1
                    last_access[fp] = today

            elif tool_name in ("Edit", "Write", "NotebookEdit"):
                fp = _extract_file_path(content, tool_name)
                if fp and _is_project_file(fp, project_roots):
                    edit_counts[fp] += 1
                    last_access[fp] = today

            elif tool_name == "Bash":
                fps = _extract_paths_from_bash(content)
                for fp in fps:
                    if _is_project_file(fp, project_roots):
                        read_counts[fp] += 1
                        last_access[fp] = today

    # merge and sort
    all_files = set(list(read_counts.keys()) + list(edit_counts.keys()))
    result = []
    for fp in all_files:
        r = read_counts.get(fp, 0)
        e = edit_counts.get(fp, 0)
        weight = r + e * 2  # edits count more
        result.append({
            "file": fp,
            "reads": r,
            "edits": e,
            "weight": weight,
            "last_access": last_access.get(fp, ""),
        })
    result.sort(key=lambda x: -x["weight"])
    return result


def _extract_file_path(content: str, tool_name: str) -> str | None:
    """Try to extract file_path from tool_use content (JSON or plain text)."""
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            for key in ("file_path", "notebook_path", "path"):
                if key in obj:
                    return str(obj[key])
    except Exception:
        pass
    # fallback: regex for absolute paths
    m = re.search(r'["\']?(/[\w/.\-_]+\.\w+)["\']?', content)
    if m:
        return m.group(1)
    return None


def _extract_paths_from_bash(content: str) -> list[str]:
    """Extract file paths from bash command content."""
    paths = []
    try:
        obj = json.loads(content)
        cmd = obj.get("command", "") if isinstance(obj, dict) else ""
    except Exception:
        cmd = content
    for m in re.finditer(r'(/[\w/.\-_]+\.(?:py|cpp|h|mlir|json|yaml|yml|md|sh|txt))', cmd):
        paths.append(m.group(1))
    return paths


# ── Markdown document builders ────────────────────────────────────────────────

def _read_existing(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def build_hotfiles_md(hotfiles: list[dict], max_items: int = 20) -> str:
    top = hotfiles[:max_items]
    if not top:
        return "## 热点文件（最近 7 天）\n\n暂无数据\n"
    lines = ["## 热点文件（最近 7 天）\n", "| 文件 | 读取 | 编辑 | 最近访问 |", "|------|------|------|----------|"]
    for f in top:
        lines.append(f"| {f['file']} | {f['reads']} | {f['edits']} | {f['last_access']} |")
    return "\n".join(lines) + "\n"


# ── LLM merge ─────────────────────────────────────────────────────────────────

async def _llm_call(command: str, prompt: str, timeout: int = 120) -> str:
    """Call an LLM command with -p flag and stream-json output, return result text."""
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
        logger.warning("LLM call timed out (command=%s)", command)
        return ""
    except Exception:
        logger.exception("LLM call failed (command=%s)", command)
        return ""


async def merge_errors(existing_md: str, signals: list[dict], command: str, cfg: dict) -> str:
    if not signals:
        return existing_md

    max_items = cfg.get("errors_max_items", 10)
    max_chars = cfg.get("errors_max_chars", 2000)

    # Build signal text
    signal_parts = []
    for s in signals[:30]:  # limit input
        label = f"[{s['source']}]"
        if s.get("task_failed"):
            label += "[task_failed]"
        if s.get("high_turns"):
            label += "[high_turns]"
        signal_parts.append(f"{label} {s['content']}")
    signal_text = "\n---\n".join(signal_parts)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""你是一个经验分析器。从以下对话记录中提取「错误 → 正确做法」的经验对。

## 输入

### 当前已有的错误经验文档
{existing_md or "（暂无）"}

### 新的对话记录（仅含错误相关片段）
{signal_text}

## 要求

1. 从新对话中识别：用户纠正了什么、哪里执行失败了、哪里走了弯路
2. 提取为「错误描述 → 正确做法」的结构
3. 与已有文档合并：相同错误模式合并，出现次数累加
4. 按频率 × 严重程度排序
5. 最多保留 {max_items} 条，不超过 {max_chars} 字符
6. 输出纯 Markdown，无需代码块包裹
7. 今天日期是 {today}

## 输出格式

## 错误经验

### 1. [错误简述]
- **错误**: ...
- **正确做法**: ...
- **出现次数**: N
- **最近发生**: YYYY-MM-DD
"""
    result = await _llm_call(command, prompt)
    if not result:
        return existing_md
    return result


async def merge_project(existing_md: str, signals: list[dict], command: str, cfg: dict) -> str:
    if not signals:
        return existing_md

    max_items = cfg.get("project_max_items", 15)
    max_chars = cfg.get("project_max_chars", 2000)

    signal_parts = []
    for s in signals[:20]:  # tighter limit for better signal/noise
        signal_parts.append(f"[{s['source']}][task:{s['task_name']}] {s['content']}")
    signal_text = "\n---\n".join(signal_parts)

    prompt = f"""你是一个知识整理器。从以下对话记录中提取可复用的项目知识。

**重要约束**：
- 这些对话来自用户使用某个外部项目（如编译器、ML 框架等）时的记录
- 你要提取的是**用户工作的那个外部项目**的知识（文件结构、命令、约定、关键文件等）
- 严禁输出关于「对话管理系统」「agent-park」「知识总结」本身的内容
- 如果对话内容是关于 agent-park/knowledge.py/memory 等系统内部实现，直接返回已有文档，不做修改

## 输入

### 当前已有的项目知识文档
{existing_md or "（暂无）"}

### 新的对话记录（仅含知识相关片段）
{signal_text}

## 要求

1. 从新对话中识别：项目目录结构、文件用途、构建/运行命令、团队约定、关键路径
2. 与已有文档合并：同一主题用最新信息覆盖旧的
3. 保持分类结构（目录结构、常用命令、约定、关键文件）
4. 最多保留 {max_items} 条知识点，不超过 {max_chars} 字符
5. 只保留对后续任务有直接帮助的信息，去掉一次性的具体细节
6. 输出纯 Markdown，无需代码块包裹
7. 每条知识点以「- 」开头

## 输出格式

按分类组织，每条知识点简洁明确，可直接作为 agent 的参考信息。

## 项目知识

"""
    result = await _llm_call(command, prompt)
    if not result:
        return existing_md
    return result


# ── Memory index update ───────────────────────────────────────────────────────

def build_memory_entries(eid: str, errors_md: str, project_md: str, hotfiles: list[dict]) -> list[dict]:
    """Build memory entries from knowledge documents (one entry per knowledge point)."""
    from server.memory import _utcnow_iso
    ts = _utcnow_iso()
    entries = []

    # Parse errors.md into individual entries
    if errors_md and "暂无" not in errors_md and errors_md.strip():
        # Each "### N." block is one error
        blocks = re.split(r'\n### \d+\.', errors_md)
        for block in blocks[1:]:  # skip header
            lines = block.strip().splitlines()
            if not lines:
                continue
            title = lines[0].strip()
            # Extract 正确做法
            correct = ""
            for line in lines:
                if "正确做法" in line:
                    correct = re.sub(r'.*\*\*正确做法\*\*:\s*', '', line).strip()
                    break
            summary = f"[错误经验] {title}"
            if correct:
                summary += f"。正确做法：{correct}"
            summary += f"。详见 data/knowledge/{eid}/errors.md"
            entries.append({
                "type": "knowledge_summary",
                "timestamp": ts,
                "content": summary,
            })

    # Parse project.md into entries (bullet points or ### section headings)
    if project_md and "暂无" not in project_md and project_md.strip():
        current_section = ""
        for line in project_md.splitlines():
            stripped = line.strip()
            # Track section headings for context
            if stripped.startswith("### "):
                current_section = stripped.lstrip("# ").strip()
                continue
            if stripped.startswith("## "):
                current_section = stripped.lstrip("# ").strip()
                continue
            # Bullet points (- or *)
            if (stripped.startswith("- ") or stripped.startswith("* ")) and len(stripped) > 10:
                content = stripped[2:].strip()
                if content:
                    label = f"[{current_section}] " if current_section else ""
                    entries.append({
                        "type": "knowledge_summary",
                        "timestamp": ts,
                        "content": f"[项目知识]{label}{content}。详见 data/knowledge/{eid}/project.md",
                    })

    # One entry for hotfiles summary
    if hotfiles:
        top3 = hotfiles[:3]
        summary_parts = [f"{f['file']}(读{f['reads']}/改{f['edits']})" for f in top3]
        entries.append({
            "type": "knowledge_summary",
            "timestamp": ts,
            "content": f"[热点文件] 近期高频文件: {', '.join(summary_parts)}。详见 data/knowledge/{eid}/hotfiles.md",
        })

    return entries


def update_memory_index(agent_id: str, entries: list[dict]) -> None:
    """Delete old knowledge_summary entries and write new ones."""
    from server.memory import effective_memory_agent_id, _memory_path, MEMORY_DIR

    eid = effective_memory_agent_id(agent_id)
    path = _memory_path(eid)

    # Read existing lines, filter out old knowledge_summary entries
    existing_lines: list[str] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") == "knowledge_summary":
                    continue  # remove old entries
            except Exception:
                pass
            existing_lines.append(line)

    # Append new entries
    new_lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    all_lines = existing_lines + new_lines

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(all_lines) + ("\n" if all_lines else ""), encoding="utf-8")
    logger.info("Updated memory index for agent %s: %d knowledge entries", agent_id, len(entries))


# ── Write documents ───────────────────────────────────────────────────────────

def write_knowledge_docs(agent_id: str, errors_md: str, project_md: str, hotfiles_md: str) -> None:
    kdir = knowledge_dir(agent_id)
    kdir.mkdir(parents=True, exist_ok=True)
    (kdir / "errors.md").write_text(errors_md, encoding="utf-8")
    (kdir / "project.md").write_text(project_md, encoding="utf-8")
    (kdir / "hotfiles.md").write_text(hotfiles_md, encoding="utf-8")
    logger.info("Wrote knowledge docs for agent %s at %s", agent_id, kdir)


def read_knowledge_docs(agent_id: str) -> dict[str, str]:
    kdir = knowledge_dir(agent_id)
    return {
        "errors": _read_existing(kdir / "errors.md"),
        "project": _read_existing(kdir / "project.md"),
        "hotfiles": _read_existing(kdir / "hotfiles.md"),
    }


# ── Main entry point ──────────────────────────────────────────────────────────

async def generate_summary(
    agent_id: str,
    tasks: list,
    progress_cb=None,
) -> dict:
    """Main entry: extract, merge, write docs, update memory index.

    progress_cb(step, detail) is called with progress updates if provided.
    Returns {"files_updated": [...], "memory_entries": N}
    """
    from server.config import knowledge_config

    cfg = knowledge_config()
    command = cfg.get("command", "minimax")
    recent_days = cfg.get("hotfiles_recent_days", 7)
    hotfiles_max = cfg.get("hotfiles_max_items", 20)

    eid = effective_knowledge_agent_id(agent_id)

    async def progress(step: str, detail: str):
        if progress_cb:
            await progress_cb(step, detail)

    await progress("extracting", f"分析 {len(tasks)} 个任务...")

    # Step 1: Extract signals (no LLM)
    error_signals = extract_error_signals(tasks)
    project_signals = extract_project_signals(tasks)
    from server.state import app_state

    agent = app_state.get_agent(agent_id)
    project_root = (agent.cwd or "").strip() if agent else ""
    hotfiles = compute_hotfiles(tasks, recent_days, project_root=project_root or None)

    await progress("extracting", f"提取到 {len(error_signals)} 条错误信号，{len(project_signals)} 条知识信号，{len(hotfiles)} 个文件")

    # Read existing docs
    existing = read_knowledge_docs(agent_id)

    # Step 2: LLM merge
    await progress("merging", "合并 errors.md...")
    new_errors_md = await merge_errors(existing["errors"], error_signals, command, cfg)

    await progress("merging", "合并 project.md...")
    new_project_md = await merge_project(existing["project"], project_signals, command, cfg)

    # hotfiles: no LLM
    new_hotfiles_md = build_hotfiles_md(hotfiles, hotfiles_max)

    # Step 3: Write docs
    await progress("writing", "写入知识文档...")
    write_knowledge_docs(agent_id, new_errors_md, new_project_md, new_hotfiles_md)

    # Build memory entries
    mem_entries = build_memory_entries(eid, new_errors_md, new_project_md, hotfiles)
    update_memory_index(agent_id, mem_entries)

    files_updated = []
    if new_errors_md != existing["errors"]:
        files_updated.append("errors.md")
    if new_project_md != existing["project"]:
        files_updated.append("project.md")
    files_updated.append("hotfiles.md")

    return {
        "files_updated": files_updated,
        "memory_entries": len(mem_entries),
    }
