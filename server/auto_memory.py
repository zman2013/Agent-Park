"""Auto-memory: layered, per-effective-id agent memory.

The *effective id* (eid) groups agents that share one memory store — several
worktree agents on the same project consolidate into a single set of documents.

This module owns the single definition of that grouping. ``memory.py`` and
``knowledge.py`` re-export from here so the rule cannot drift between the two.

Four layers live under ``data/memory/{eid}/``, ordered by how much authority
they carry:

===========  ========  ==================  ===============================
layer        priority  authority           written by
===========  ========  ==================  ===============================
profile.md   1         the user            humans only — never the LLM
lessons.md   2         corrections         consolidation, JSON delta
project.md   3         observation         consolidation, JSON delta
hotfiles.md  4         none (statistics)   pure Python, no LLM
===========  ========  ==================  ===============================

Consolidation never writes ``profile.md``. It is the one human-authored
source, and a system that silently edits the user's own rules is not one they
can keep trusting.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MEMORY_DIR = DATA_DIR / "memory"


def effective_id(agent_id: str) -> str:
    """Return the id of the agent whose memory store *agent_id* reads/writes.

    Falls back to *agent_id* itself when no sharing is configured, when the
    agent is unknown, or when ``shared_memory_agent_id`` points at a
    nonexistent agent. Resolution is a single hop by design: chains would let
    a config typo silently redirect an entire project's memory.
    """
    from server.state import app_state

    agent = app_state.get_agent(agent_id)
    if not agent:
        return agent_id
    target = agent.shared_memory_agent_id
    if not target:
        return agent_id
    if target not in app_state.agents:
        logger.warning(
            "Agent %s shares memory with unknown agent %s; using own id",
            agent_id, target,
        )
        return agent_id
    return target


def eid_members(eid: str) -> list[str]:
    """Return the ids of every agent that maps to *eid*, archived included.

    Callers that consolidate must aggregate across all members: reading only
    one member's tasks and then overwriting the shared documents discards the
    other members' data.
    """
    from server.state import app_state

    return [aid for aid in app_state.agents if effective_id(aid) == eid]


def active_eids() -> list[str]:
    """Return every eid that has at least one non-archived member agent.

    Archived agents are excluded because their history is frozen: re-running
    consolidation over them burns LLM calls and rewrites documents for work
    that will never continue.
    """
    from server.state import app_state

    seen: dict[str, None] = {}
    for aid, agent in app_state.agents.items():
        if agent.archived:
            continue
        seen.setdefault(effective_id(aid), None)
    return list(seen)


# ── Layers ────────────────────────────────────────────────────────────────────

# Per-layer character ceilings. Deliberately generous: today's largest real
# document is ~3KB and all eids together total ~33KB, so these are 5-10x the
# current content and are not expected to bind. They exist as a backstop, and
# each one is interpolated into its extraction prompt so the model knows the
# budget rather than having Python silently truncate what it returned.
LAYER_LIMITS = {
    "profile": 2000,
    "lessons": 8000,
    "project": 10000,
    "hotfiles": 4000,
}

LAYERS = ("profile", "lessons", "project", "hotfiles")


def memory_dir(eid: str) -> Path:
    """Return the directory holding *eid*'s layer documents.

    Named by eid, and a directory rather than the flat ``{eid}.jsonl`` files
    that live alongside it — so migration adds files instead of replacing them
    and the old path stays readable for rollback.
    """
    return MEMORY_DIR / eid


def layer_path(eid: str, layer: str) -> Path:
    return memory_dir(eid) / f"{layer}.md"


def read_layer(eid: str, layer: str) -> str:
    path = layer_path(eid, layer)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed to read %s layer for eid %s", layer, eid)
        return ""


def write_layer(eid: str, layer: str, content: str) -> None:
    """Write a layer document atomically.

    The injection side reads these files on every task start, concurrently with
    consolidation; a plain write would let it observe a half-written document.
    """
    d = memory_dir(eid)
    d.mkdir(parents=True, exist_ok=True)
    path = layer_path(eid, layer)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ── Entry model ───────────────────────────────────────────────────────────────

# id:  md5(normalized title)[:6] — stable across runs so a repeat sighting
#      increments n instead of adding a near-duplicate entry.
# n:   times seen. last: most recent date seen.
_ENTRY_RE = re.compile(
    r"^## (?P<title>.+?)\s*<!-- id:(?P<id>[0-9a-f]{6})"
    r"(?:\s+n:(?P<n>\d+))?(?:\s+last:(?P<last>[\d-]+))?\s*-->\s*$"
)


def entry_id(title: str) -> str:
    """Return the stable id for an entry title.

    Normalization is deliberately shallow — case, whitespace and trailing
    punctuation only. Anything cleverer (stemming, synonyms) would make ids
    unstable across library versions, and the semantic merging of two
    differently-worded entries is the delta LLM's job via ``op:"update"``.
    """
    norm = re.sub(r"\s+", " ", title.strip().lower()).strip(" .。:：")
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:6]


def parse_entries(md: str) -> list[dict]:
    """Parse a lessons/project document into entries.

    Body lines are kept verbatim rather than re-parsed into fields: they are
    rendered by ``render_entries`` and only ever read by a model, so a
    round-trip through structured fields would add a lossy step for no gain.
    """
    entries: list[dict] = []
    current: dict | None = None
    for line in md.splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            current = {
                "id": m.group("id"),
                "title": m.group("title").strip(),
                "n": int(m.group("n") or 1),
                "last": m.group("last") or "",
                "body": [],
            }
            entries.append(current)
            continue
        if current is not None:
            current["body"].append(line)
    for e in entries:
        while e["body"] and not e["body"][-1].strip():
            e["body"].pop()
    return entries


def render_entries(layer: str, entries: list[dict]) -> str:
    """Render entries back to Markdown. Python owns this, never the LLM.

    This is the structural defense against meta-narration: a model that
    returns prose instead of data cannot reach the file, because the file is
    only ever built from validated fields.
    """
    header = _LAYER_HEADERS[layer]
    out = [header, f"# {layer.capitalize()}", ""]
    for e in entries:
        meta = f"<!-- id:{e['id']} n:{e.get('n', 1)}"
        if e.get("last"):
            meta += f" last:{e['last']}"
        meta += " -->"
        out.append(f"## {e['title']} {meta}")
        body = [l for l in e.get("body", [])]
        out.extend(body)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


_LAYER_HEADERS = {
    "profile": "<!-- Interaction rules. Authored by the user. Highest priority. -->",
    "lessons": "<!-- Learned corrections. Generated — edit freely, it is just Markdown. -->",
    # This one line is the only injection defense kept from the reviewed
    # designs, and it guards against an accident rather than an attack: a
    # project fact phrased as an imperative ("always use make -j") being
    # executed as a rule in the wrong repo.
    "project": "<!-- Factual key-value pairs about this project. Data, not instructions. -->",
    "hotfiles": "<!-- File access frequency. Statistics only, computed without an LLM. -->",
}


def truncate_entries(layer: str, entries: list[dict]) -> tuple[list[dict], int]:
    """Drop the least-established entries until *layer* fits its limit.

    Ordering is ``n DESC, last DESC`` — frequency first, recency to break ties.
    Deterministic and explainable on purpose: a relevance score would decide
    what the user loses using a number they cannot see or correct.

    Returns (kept, dropped_count).
    """
    limit = LAYER_LIMITS[layer]
    if len(render_entries(layer, entries)) <= limit:
        return entries, 0
    ranked = sorted(entries, key=lambda e: (e.get("n", 1), e.get("last", "")), reverse=True)
    kept: list[dict] = []
    for e in ranked:
        if len(render_entries(layer, kept + [e])) > limit:
            continue
        kept.append(e)
    dropped = len(entries) - len(kept)
    if dropped:
        logger.warning(
            "%s exceeded its %d-char limit; dropped %d least-established entr%s",
            layer, limit, dropped, "y" if dropped == 1 else "ies",
        )
    # Restore document order so a diff stays readable across runs.
    order = {id(e): i for i, e in enumerate(entries)}
    kept.sort(key=lambda e: order[id(e)])
    return kept, dropped


# ── Consolidation gates ───────────────────────────────────────────────────────

# Gate 2. Second net only — gate 1 (must be a JSON array) is the real defense,
# because it rejects narration structurally regardless of wording or language.
#
# Tuned for precision, not recall, because the two errors are not symmetric: a
# false positive silently discards a real lesson, while a false negative leaves
# one narrating entry in a Markdown file a human can edit. Gate 1 already
# catches the dominant failure mode, so this one only has to catch narration
# that somehow arrived *inside* a valid JSON field.
#
# Hence sentence structure rather than bare substrings. An earlier version
# matched any occurrence of 本轮 or 已合并 and refused two legitimate facts on a
# live run: 「…必须以本轮起始消息位置为扫描边界…」 (a real technical
# constraint) and 「已合并分支不再堆新改动」 (已合并分支 is a noun phrase, not a
# claim about the extraction).
#
# The positive cases are measured, not guessed. A first version reported
# "0/6 polluted" on a real run and was wrong: reading the files by hand turned up
# 「分析完成。本轮对话片段中的信号其实很弱……」 (the pattern had 分析对话 but not
# 分析完成) and an entirely English narration, "Verified against the actual skill
# scripts. Writing the merged knowledge doc." — the blacklist was Chinese-only.
_META_PATTERNS = (
    # Narration announcing the extraction is done, at the start of the text.
    r"^\s*[-*]?\s*(分析|提取|整理|合并|处理)(完成|完毕)",
    r"^\s*[-*]?\s*(以下是|这是)(我|本次)?(提取|识别|整理|分析)",
    r"^\s*[-*]?\s*根据(约束|以上|上述|对话)",
    # Claims about what this run changed.
    r"(未提取|无可提取|无新增|无需更新|无需修改)",
    r"本次(提取|合并|变化|更新|分析)",
    r"本轮(提取|合并|分析)(到|出|了)",
    r"我(识别出|提取了|分析了|合并了)",
    r"(识别出|提取了|整理了)以下",
    r"已(将|把).{0,30}(合并|更新|写入|整理)(到|进|了)?(已有|现有)?文档",
    r"文档已(更新|生成|合并)",
    # Our own prompt's vocabulary showing up in the answer.
    r"已有文档",
    # English narration. Anchored *and* requiring a first-person or
    # document-referring object: a fact legitimately starting with "Updated" or
    # "Merged" is ordinary technical writing ("Updated dependencies must be
    # written to requirements.txt"), so the bare verb cannot be the signal.
    r"(?i)^\s*[-*]?\s*(i (found|identified|analyzed|extracted|merged|updated)\b|"
    r"here'?s (the|what|my)\b|"
    r"(verified|analyzed|merged|updated|extracted)( against| the| all){0,2} ?"
    r"(above|following|existing|conversation|document|knowledge|entries|items|actual)\b|"
    r"writing the (merged|updated|new) \b|"
    r"the (document|file) (has been|is now|was) (updated|merged|written)\b|"
    r"no (new |additional )?(knowledge|lessons|entries|items|changes|updates)\b)",
)
_META_RE = tuple(re.compile(p) for p in _META_PATTERNS)


def is_meta_narration(text: str) -> bool:
    """True when *text* talks about the extraction instead of stating content."""
    return any(r.search(text or "") for r in _META_RE)


class ConsolidationResult(dict):
    """Counts for one layer's consolidation.

    ``refused`` and ``failed`` are kept apart on purpose. The old code returned
    ``existing_md`` on timeout, which made "the LLM produced nothing usable"
    and "there was nothing to change" the same observable outcome — so a
    pipeline that had been silently dead for five months looked healthy.
    """

    def __init__(self, added=0, updated=0, deleted=0, refused=0, failed=False, dropped=0):
        super().__init__(added=added, updated=updated, deleted=deleted,
                         refused=refused, failed=failed, dropped=dropped)

    def __str__(self) -> str:
        s = (f"added={self['added']} updated={self['updated']} "
             f"deleted={self['deleted']} refused={self['refused']}")
        if self["dropped"]:
            s += f" dropped={self['dropped']}"
        if self["failed"]:
            s += " FAILED(no usable output)"
        return s


def apply_delta(
    layer: str,
    entries: list[dict],
    ops: list,
    today: str,
) -> tuple[list[dict], ConsolidationResult]:
    """Apply validated delta *ops* to *entries*. Gates 2-4 live here.

    Every op is checked independently; a bad one is dropped and counted in
    ``refused`` rather than aborting the batch, so one malformed item cannot
    discard the good ones alongside it.
    """
    by_id = {e["id"]: e for e in entries}
    res = ConsolidationResult()

    for op in ops:
        if not isinstance(op, dict):
            res["refused"] += 1
            continue
        kind = op.get("op")
        title = str(op.get("title") or "").strip()
        body = _op_body(op)

        # Gate 2: meta-narration in any user-visible field.
        if is_meta_narration(title) or any(is_meta_narration(b) for b in body):
            logger.warning("%s: refused meta-narration op: %.120s", layer, op)
            res["refused"] += 1
            continue

        if kind == "delete":
            # Gate 3: an id we never wrote means the model invented it.
            target = by_id.get(str(op.get("id", "")))
            if target is None:
                res["refused"] += 1
                continue
            entries.remove(target)
            del by_id[target["id"]]
            res["deleted"] += 1
            continue

        if not title or not body:
            res["refused"] += 1
            continue

        if kind == "update":
            target = by_id.get(str(op.get("id", "")))
            if target is None:
                res["refused"] += 1
                continue
            target["title"] = title
            target["body"] = body
            target["last"] = today
            res["updated"] += 1
            continue

        if kind == "add":
            new_id = entry_id(title)
            # Gate 3: a repeat sighting bumps n instead of adding a twin.
            if new_id in by_id:
                dup = by_id[new_id]
                dup["n"] = dup.get("n", 1) + 1
                dup["last"] = today
                res["updated"] += 1
                continue
            e = {"id": new_id, "title": title, "n": 1, "last": today, "body": body}
            entries.append(e)
            by_id[new_id] = e
            res["added"] += 1
            continue

        res["refused"] += 1

    # Gate 4: total budget.
    entries, dropped = truncate_entries(layer, entries)
    res["dropped"] = dropped
    return entries, res


def _op_body(op: dict) -> list[str]:
    """Render an op's content fields into Markdown body lines.

    lessons ops carry wrong/right; project ops carry a single fact. Both shapes
    become plain bullets here — Python decides the Markdown, never the model.
    """
    wrong = str(op.get("wrong") or "").strip()
    right = str(op.get("right") or "").strip()
    if wrong or right:
        lines = []
        if wrong:
            lines.append(f"- 错误：{wrong}")
        if right:
            lines.append(f"- 正确：{right}")
        return lines
    fact = str(op.get("fact") or "").strip()
    return [f"- {fact}"] if fact else []


def parse_delta_ops(raw: str, layer: str) -> list | None:
    """Gate 1: the output must be a JSON array, or nothing is applied.

    Returns None when the output is unusable, which callers must treat as a
    failure — *not* as an empty delta. An empty delta means the model looked and
    found nothing; None means we never got an answer, and writing the document
    on that basis is how a bad run overwrites a good document.
    """
    from server.wiki_ingest import _parse_json_from_llm_output

    if not raw.strip():
        return None
    parsed = _parse_json_from_llm_output(raw)
    if not isinstance(parsed, list):
        logger.warning(
            "%s: discarding non-array LLM output (%s): %.500s",
            layer, type(parsed).__name__, raw,
        )
        return None
    return parsed


# ── Extraction prompts ────────────────────────────────────────────────────────

# There is deliberately no "if the conversation is about X, return the existing
# document unchanged" escape hatch. The old prompt had one, and the LLM obeyed it
# the only way it could — by narrating that it was returning the document
# unchanged, which then *became* the document. The escape hatch caused the exact
# pollution it was meant to prevent. Returning [] is the only way out now.
_LESSONS_PROMPT = """你是错误经验提取器。从对话片段中提取「错误 → 正确做法」，输出对已有条目的增量操作。

## 已有条目
{existing}

## 新对话片段
{signals}

## 输出
只输出 JSON 数组。无可提取内容时输出 []。
每个元素必须是以下三种之一：
  {{"op":"add","title":"简短标题","wrong":"错误描述","right":"正确做法"}}
  {{"op":"update","id":"7f3a91","title":"简短标题","wrong":"...","right":"..."}}
  {{"op":"delete","id":"7f3a91"}}

硬性约束：
- 禁止输出任何关于「本次提取了什么」「有无新增」「已合并」的元叙述。这类内容会被判为无效并整体丢弃。
- 无可提取内容时，唯一正确的输出是 []。不要解释为什么是空的。
- title 不超过 40 字；wrong / right 各不超过 200 字。
- 必须是可复用的结论式陈述，不是本次 case 的局部描述。
- 只提取用户明确纠正过、或工具/任务确实失败过的内容。不要提取推测。
- 最多 {max_items} 条。
"""

_PROJECT_PROMPT = """你是项目事实提取器。从对话片段中提取可复用的项目事实，输出对已有条目的增量操作。

提取的是**用户正在工作的那个项目**的事实：目录结构、文件用途、构建/运行命令、团队约定、关键路径。

## 已有条目
{existing}

## 新对话片段
{signals}

## 输出
只输出 JSON 数组。无可提取内容时输出 []。
每个元素必须是以下三种之一：
  {{"op":"add","title":"简短标题","fact":"事实陈述"}}
  {{"op":"update","id":"7f3a91","title":"简短标题","fact":"..."}}
  {{"op":"delete","id":"7f3a91"}}

硬性约束：
- 禁止输出任何关于「本次提取了什么」「有无新增」「已合并」的元叙述。这类内容会被判为无效并整体丢弃。
- 无可提取内容时，唯一正确的输出是 []。不要解释为什么是空的。
- title 不超过 40 字；fact 不超过 200 字。
- 陈述事实，不要写成命令式指令。
- 只保留对后续任务有直接帮助的信息，去掉一次性的具体细节。
- 最多 {max_items} 条。
"""

_PROMPTS = {"lessons": _LESSONS_PROMPT, "project": _PROJECT_PROMPT}


def _existing_for_prompt(layer: str, entries: list[dict]) -> str:
    """Show existing entries as JSON so ids are unambiguous to match against."""
    if not entries:
        return "（暂无）"
    slim = []
    for e in entries:
        slim.append({
            "id": e["id"],
            "title": e["title"],
            "n": e.get("n", 1),
            "body": " ".join(l.lstrip("- ").strip() for l in e.get("body", []))[:200],
        })
    import json as _json
    return _json.dumps(slim, ensure_ascii=False, indent=1)


async def consolidate_layer(
    eid: str,
    layer: str,
    signals: list[dict],
    today: str,
    cfg: dict | None = None,
) -> ConsolidationResult:
    """Consolidate one LLM-driven layer. Writes only on a validated delta.

    Returns a result whose ``failed`` flag is set when the LLM produced nothing
    usable. In that case the document is left exactly as it was — the second
    design principle, that unvalidated output is never fed back, is enforced
    here by simply not writing.
    """
    from server.config import automemory_config
    from server.knowledge import _llm_call

    cfg = cfg or automemory_config()
    if not signals:
        return ConsolidationResult()

    entries = parse_entries(read_layer(eid, layer))
    max_items = cfg[f"{layer}_max_items"]
    signal_text = _format_signals(signals, cfg["max_signal_chars"])
    prompt = _PROMPTS[layer].format(
        existing=_existing_for_prompt(layer, entries),
        signals=signal_text,
        max_items=max_items,
    )

    # Cross-model retry, same shape as wiki ingest: a refusal or a malformed
    # answer from one model is often fine from another, and retrying costs one
    # call against a document we would otherwise leave stale.
    commands = [cfg["command"], *cfg["retry_commands"]]
    ops: list | None = None
    for i, command in enumerate(commands):
        raw = await _llm_call(command, prompt, timeout=cfg["timeout"])
        ops = parse_delta_ops(raw, layer)
        if ops is not None:
            if i:
                logger.info("%s/%s: recovered on retry with %s", eid, layer, command)
            break
        logger.warning("%s/%s: %s produced no usable delta", eid, layer, command)

    if ops is None:
        logger.warning(
            "%s/%s: every command failed; leaving the document untouched", eid, layer
        )
        return ConsolidationResult(failed=True)

    entries, res = apply_delta(layer, entries, ops, today)
    # An all-refused batch is not "no changes": nothing was validated, so
    # writing would persist a truncation as though it were a decision.
    if res["added"] or res["updated"] or res["deleted"] or res["dropped"]:
        write_layer(eid, layer, render_entries(layer, entries))
    logger.info("%s/%s: %s", eid, layer, res)
    return res


def _format_signals(signals: list[dict], max_chars: int) -> str:
    """Join signals into prompt text, stopping at *max_chars*.

    Truncation is logged rather than silent: a run that saw half its input
    should not read like a run that saw all of it.
    """
    parts: list[str] = []
    total = 0
    for i, s in enumerate(signals):
        label = f"[{s.get('source', '?')}]"
        for flag in ("task_failed", "high_turns"):
            if s.get(flag):
                label += f"[{flag}]"
        piece = f"{label} {s.get('content', '')}"
        if total + len(piece) > max_chars:
            logger.info(
                "signal text hit the %d-char cap; used %d of %d signals",
                max_chars, i, len(signals),
            )
            break
        parts.append(piece)
        total += len(piece)
    return "\n---\n".join(parts)


# ── Top-level consolidation ───────────────────────────────────────────────────

_eid_locks: dict[str, "object"] = {}


async def consolidate(
    eid: str,
    tasks: list,
    hotfiles_tasks: list | None = None,
    progress_cb=None,
) -> dict:
    """Consolidate every layer for *eid*.

    *tasks* feeds LLM extraction (usually one day's worth). *hotfiles_tasks*
    feeds the file-heat statistics, which keep a multi-day window and must span
    every agent sharing this store — computing them from one member and then
    overwriting the shared document discards the rest.

    ``profile.md`` is never touched: it is the user's own file.
    """
    import asyncio
    from datetime import datetime, timezone

    from server.config import automemory_config, knowledge_config
    from server.knowledge import compute_hotfiles, extract_project_signals
    from server.state import app_state

    cfg = automemory_config()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hotfiles_tasks = tasks if hotfiles_tasks is None else hotfiles_tasks

    async def progress(step: str, detail: str):
        if progress_cb:
            await progress_cb(step, detail)

    lock = _eid_locks.setdefault(eid, asyncio.Lock())
    async with lock:
        await progress("extracting", f"分析 {len(tasks)} 个任务...")
        lesson_signals = extract_lesson_signals(tasks)
        project_signals = extract_project_signals(tasks)

        agent = app_state.get_agent(eid)
        project_root = (agent.cwd or "").strip() if agent else ""
        kcfg = knowledge_config()
        hotfiles = compute_hotfiles(
            hotfiles_tasks, kcfg["hotfiles_recent_days"], project_root=project_root or None
        )
        await progress(
            "extracting",
            f"提取到 {len(lesson_signals)} 条经验信号，"
            f"{len(project_signals)} 条项目信号，{len(hotfiles)} 个文件",
        )

        results: dict[str, ConsolidationResult] = {}
        await progress("merging", "巩固 lessons.md...")
        results["lessons"] = await consolidate_layer(eid, "lessons", lesson_signals, today, cfg)
        await progress("merging", "巩固 project.md...")
        results["project"] = await consolidate_layer(eid, "project", project_signals, today, cfg)

        # hotfiles: pure statistics, no LLM. This is the only layer that has
        # never been polluted, across two independent measurements.
        await progress("writing", "写入 hotfiles.md...")
        write_layer(eid, "hotfiles", _hotfiles_doc(hotfiles, kcfg["hotfiles_max_items"]))

    totals = {k: sum(r[k] for r in results.values())
              for k in ("added", "updated", "deleted", "refused", "dropped")}
    return {
        "eid": eid,
        "layers": {k: dict(v) for k, v in results.items()},
        "failed_layers": [k for k, v in results.items() if v["failed"]],
        **totals,
    }


def _hotfiles_doc(hotfiles: list[dict], max_items: int) -> str:
    from server.knowledge import build_hotfiles_md

    return f"{_LAYER_HEADERS['hotfiles']}\n{build_hotfiles_md(hotfiles, max_items)}"


# ── Signal extraction ─────────────────────────────────────────────────────────

def extract_lesson_signals(tasks: list) -> list[dict]:
    """Extract fragments that plausibly contain a lesson.

    Narrower than the old ``extract_error_signals``, which accepted *any* user
    message between 5 and 200 characters as a "correction" — that made almost
    every message a signal, and a prompt built from noise is what the model
    then narrated its way through. Three sources with actual semantics:

    - the task failed outright
    - a tool returned an error
    - the task took an unusually long detour (high turn count)
    """
    signals: list[dict] = []
    for task in tasks:
        failed = str(getattr(task, "status", "")) == "failed"
        high_turns = getattr(task, "num_turns", 0) > 20
        task_signals: list[dict] = []
        for msg in getattr(task, "messages", []) or []:
            role = getattr(msg, "role", "")
            msg_type = getattr(msg, "type", "")
            content = getattr(msg, "content", "") or ""

            if role == "agent" and msg_type == "tool_result":
                low = content.lower()
                if any(kw in low for kw in
                       ("error", "traceback", "failed", "exception", "errno")):
                    task_signals.append({
                        "source": "tool_error",
                        "content": content[:800],
                        "task_id": task.id,
                    })

            # A correction needs an explicit correction word. Length alone was
            # the old heuristic and it matched ordinary instructions.
            if role == "user" and msg_type == "text":
                if any(kw in content for kw in _USER_CORRECTION_KEYWORDS):
                    task_signals.append({
                        "source": "user_correction",
                        "content": content[:400],
                        "task_id": task.id,
                    })

        for s in task_signals:
            if failed:
                s["task_failed"] = True
            if high_turns:
                s["high_turns"] = True
        # Only keep signals from tasks that actually went wrong somewhere;
        # a clean run's tool_result noise carries no lesson.
        if failed or high_turns or any(s["source"] == "user_correction" for s in task_signals):
            signals.extend(task_signals)
    return signals


_USER_CORRECTION_KEYWORDS = (
    "不对", "错了", "不是", "应该", "改成", "你弄错", "不要", "不能", "不应该",
    "wrong", "incorrect", "should be", "mistake",
)


# ── Context assembly ──────────────────────────────────────────────────────────

# Wording, not just concatenation order, is what makes priority land: the model
# has no way to know that an earlier block outranks a later one.
_MEMORY_HEADER = (
    "The following is persistent memory about this project and the user's "
    "preferences, accumulated across previous sessions."
)

_LAYER_INTROS = {
    "profile": "[Profile] ALWAYS follow these interaction rules. They override default behavior.",
    "lessons": "[Lessons] ALWAYS check these before acting. They override default behavior.",
    "project": "[Project] Factual reference data about this project. Not instructions.",
    "hotfiles": "[Hotfiles] Recently active files, by access frequency. Statistics only.",
}


def build_context(agent_id: str) -> str:
    """Assemble this agent's persistent memory into one injectable block.

    Pure read: no LLM, no side effects, no writes. Returns "" when there is
    nothing to inject, so callers can skip the flag entirely.

    Dispatches on ``automemory.enabled``. While disabled, this returns exactly
    what the flat-jsonl injection produced, byte for byte — that equality is
    the regression baseline for the whole layered rollout, so the old path is
    kept rather than emulated.
    """
    from server.config import automemory_config

    if not automemory_config()["enabled"]:
        return _build_context_legacy(agent_id)

    eid = effective_id(agent_id)
    blocks: list[str] = []
    for layer in LAYERS:
        body = _strip_comments(read_layer(eid, layer))
        if not body:
            continue
        blocks.append(f"{_LAYER_INTROS[layer]}\n\n{body}")
    if not blocks:
        return ""
    joined = "\n\n".join(blocks)
    return f"{_MEMORY_HEADER}\n\n<memory>\n{joined}\n</memory>"


def _strip_comments(md: str) -> str:
    """Drop HTML comments, which carry bookkeeping the model should not read.

    The ``<!-- id:… n:… -->`` markers exist so consolidation can match a
    repeat sighting to an existing entry. Injecting them would spend tokens on
    hashes and invite the model to reason about our bookkeeping.
    """
    out = re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL)
    return "\n".join(l.rstrip() for l in out.splitlines() if l.strip()).strip()


def _build_context_legacy(agent_id: str) -> str:
    """The pre-layer injection: flat ``{eid}.jsonl`` lines in one block."""
    from server.config import memory_config
    from server.memory import load_memory

    lines = load_memory(agent_id, memory_config()["max_lines"])
    if not lines:
        return ""
    return f"{_MEMORY_HEADER}\n\n<memory>\n" + "\n".join(lines) + "\n</memory>"

