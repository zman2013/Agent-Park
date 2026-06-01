"""Prompts used by the Handoff feature.

Handoff 流程将当前 agent session 的上下文整理为 docs/handoff/ 知识树，
然后融合到全局 /data1/common/wiki/handoff/ 仓库。整体分两轮发送：

第一轮（STEP_1_AND_2）：整理 docs/handoff/ + 同步 sync-principles。
该 prompt 直接发给当前 session 的 agent，复用其上下文。

第二轮（STEP_3_MERGE）：在当前 session 内继续触发，让 agent 把
docs/handoff/ 融合到 /data1/common/wiki/handoff/。完成后由后端
发送 system 消息通知 handoff 流程结束。
"""

from __future__ import annotations


# 步骤 1：整理 docs/handoff/ 持久化 handoff 知识树
STEP_1_PROMPT = """\
Update the project's persistent handoff knowledge tree at docs/handoff/, so a fresh agent (or future you) can continue the work without re-deriving context.

The tree is hierarchical: docs/handoff/INDEX.md is the only mandatory entry point; every subdirectory has its own INDEX.md, and any topic can recursively expand from a single file into its own subdirectory as it grows. A fresh agent reads only INDEX.md first and drills down on demand.

Workflow:
1. Read docs/handoff/INDEX.md (create if missing — see below).
2. Locate the most relevant existing subtree for this session's work, or decide a new subtree is warranted.
3. Read the INDEX.md of that subtree, then any leaf files you'll be updating, before writing.
4. Update in place: append new decisions to the right INDEX's "核心摘要", add or amend leaf files for details. Update "## 当前焦点" in L0 INDEX if active work lines changed (add when starting, remove when done).
5. Honour the size limits (L0 INDEX ≤ 150 lines, any INDEX ≤ 200, any leaf ≤ 400). When a file would exceed its limit, refactor: extract details to a child file, or promote a single file to a subdirectory with its own INDEX.md. After moving files, grep the tree for stale relative-path references and fix them.
6. Update the "_Last refactor:" line on every INDEX.md you touched.

If docs/handoff/ does not exist yet, bootstrap it: create docs/handoff/INDEX.md with the template (Scope, Status, Last refactor, 核心摘要, 子主题, 本层直接内容, 当前焦点) and seed 当前焦点 from this session.

Suggest the skills the next session should use, if any, by listing them under "## 推荐 skill" inside the most relevant INDEX (not L0, unless project-wide).

Capture both unfinished work AND closed-out successes — closed work lines belong in a "已结案归档" section (or a sibling subtree of solved problems), where each entry distills the reusable lessons: root cause, the fix that worked, what was tried first and abandoned, gotchas discovered along the way. Reference commits / CLs / memforge slugs by SHA / number / slug rather than restating their content; the handoff entry's job is the *why* and *how to avoid the trap next time*, not the diff.

Do not duplicate content already captured in other artifacts (PRDs, plans, ADRs, issues, commits, diffs, memforge entries, auto-memory). Reference them by path, URL, commit SHA, or memforge slug instead. The handoff tree captures only what is not recorded elsewhere: active work lines, closed-out successes worth distilling, cross-cutting decisions without an ADR, gotchas not yet promoted to memory.

If the user passed arguments, treat them as a description of what the next session will focus on, and bias the update toward that area — drill into or create the relevant subtree rather than touching unrelated branches.
"""


# 步骤 2：同步 sync-principles
STEP_2_PROMPT = """\
执行 docs/handoff/skills/sync-principles/SKILL.md
"""


# 步骤 3：merge 到全局 wiki
STEP_3_PROMPT = """\
把 docs/handoff/ 的内容融合到 /data1/common/wiki/handoff/：
diff 两边 INDEX.md，只把本项目新增/更新的内容融合到目标仓
共享段（持久知识来源、跨切面规则）两边对齐
不删除本项目源文件——完成后两边都有副本

完成后请输出一份简短的 summary：列出本次 merge 涉及的关键文件和决策要点。
"""


def step_1_and_2() -> str:
    """合并步骤 1 和步骤 2 为一段顺序 prompt，单次发送给当前 session。"""
    return (
        "Handoff 流程 (1/2)：先执行下面这段（整理 docs/handoff/），"
        "完成后接着执行第二段（同步 sync-principles）。\n\n"
        "─── 第一步 ───\n"
        f"{STEP_1_PROMPT}\n"
        "─── 第二步 ───\n"
        f"{STEP_2_PROMPT}"
    )


def step_3() -> str:
    """步骤 3：merge 到全局 wiki。"""
    return (
        "Handoff 流程 (2/2)：现在请把本项目 docs/handoff/ 融合到全局仓。\n\n"
        f"{STEP_3_PROMPT}"
    )


# 完成 handoff 后由后端写入 task 的系统消息文案
COMPLETION_SYSTEM_MESSAGE = (
    "✅ Handoff 流程完成。\n"
    "- 步骤 1/2：已整理 docs/handoff/ 并同步 sync-principles\n"
    "- 步骤 3：已 merge 到 /data1/common/wiki/handoff/\n"
    "请检查 agent 输出的 summary 确认细节。"
)
