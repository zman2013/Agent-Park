# AgentLoop 设计文档

> 一个 CLI 脚本，通过 planner / PM / dev / qa 四个角色的顺序协作，驱动 design.md 到完整交付物。
>
> 内嵌在 agent-park 仓库 `agentloop/` 目录，作为独立 CLI 发布，不依赖 agent-park 服务运行。

---

## 1. 设计目标与非目标

### 目标

- 用户提供 `design.md` + 一行命令，自动推进到所有任务完成
- 四个角色职责明确，通过**文件契约**（design.md + todolist.md）通信，而非对话
- 运行失败时带"失败记忆"自重试，无人值守
- 所有状态落盘，随时中断、随时恢复
- 每个角色的 LLM 后端可独立配置（便宜的给 qa / PM，贵的给 planner / dev）

### 非目标

- 不是 agent-park 服务的一部分（agent-park 仅可作为只读 UI）
- 不处理多项目调度、队列、优先级（并发多项目由用户自己开多终端）
- 不做智能规划修正（design.md 全程只读，改 design 必须人类介入）
- 不做复杂的 agent 间通信协议（只通过 todolist 通信）

---

## 2. 核心架构

```
┌─────────────────────────────────────────────────────────┐
│  用户命令:  agentloop run design.md                      │
└─────────────────────────────────────────────────────────┘
                        │
                        ▼
      ┌──────────────────────────────────────────┐
      │  loop.py (纯代码调度循环)                │
      │                                           │
      │  phase 0: planner → todolist.md          │
      │  phase 1: while not done:                │
      │    ┌─ PM.decide(todolist) → {next, id}  │
      │    ├─ run_agent(next, id)                │
      │    └─ validate_transition()              │
      └──────────────────────────────────────────┘
                        │
                        ▼
      ┌──────────────────────────────────────────┐
      │  工作目录 (cwd)                           │
      │  ├── design.md        ← 输入，只读       │
      │  ├── todolist.md      ← 共享契约，可写   │
      │  └── .agentloop/                         │
      │      ├── config.toml  ← agent 后端配置   │
      │      ├── state.json   ← loop 状态        │
      │      └── runs/        ← 每轮执行日志     │
      └──────────────────────────────────────────┘
```

### 定性

- **CLI 脚本**，不是后台服务。跑完退出。
- **短命进程 + 文件状态**：`.agentloop/state.json` 和 `todolist.md` 是唯一真相源，进程退出后通过重新运行命令恢复。
- **与 agent-park 解耦**：loop 自己 `subprocess.Popen("cco" / "ccs" / ...)`，不走 agent-park 的 REST/WS。agent-park 后续可加"Project Watch"面板，只读展示 `.agentloop/`。
- **四角色分工**：planner 一次性、PM 每轮决策、dev/qa 按 item 执行。

---

## 3. 四角色职责

| 角色 | 调用时机 | 输入 | 产物 | 只读 | 可写 | LLM 调用频次 |
|---|---|---|---|---|---|---|
| **planner** | 启动时跑 1 次 | design.md | todolist.md（初始） | design.md | todolist.md | 整个项目 1 次 |
| **PM** | 每轮 1 次 | todolist.md | `Decision` JSON | design.md, todolist.md | — | 每轮 1 次（首版为代码版，无 LLM）|
| **dev** | 被 PM 指派时 | design.md + 指定 item + attempt_log | 代码变更 + 更新自己 item | design.md | todolist.md（仅自己 item）, 代码 | 每次 1 次 |
| **qa** | 被 PM 指派时 | design.md + 指定 item + 代码 diff | findings + 追加新 item | design.md, 代码, todolist.md | todolist.md（追加 + 改 ready→done/pending） | 每次 1 次 |

### 关键约束

- **design.md 全程只读**，没有任何 agent 能改它。QA 发现 design 本身有问题 → 追加 `type:design-update` item → 触发人类介入（非运行时，由 loop exhausted 后人工处理）
- **planner 只在 phase 0 跑一次**。中途需要重新规划等同于人类介入（删除 `.agentloop/` + 修改 design + 重跑）
- **agent 间不共享对话历史**，只通过 todolist 通信（context quarantine）

---

## 4. Todolist Schema

### 文件格式

```markdown
---
project: <name>
design_doc: design.md
created_at: 2026-04-29T10:00:00Z
cycle: 3
---

# Todolist

## Items

### T-001 · type:dev · status:done
实现 POST /foo 路由骨架
- dependencies: []
- dev_notes: 基于 fastapi.APIRouter，仅返回 200 空体

### T-002 · type:qa · status:done
检查 T-001 的返回格式符合 design §3.2
- source: follows T-001
- findings: 无

### T-003 · type:dev · status:pending
添加 payload 校验
- dependencies: [T-001]
- attempt_log:
  - cycle 4: ready_for_qa (dev_notes: 初版实现)
  - cycle 6: pending (qa findings: 缺少 email 格式校验 → T-007)
  - cycle 8: pending (qa findings: email 校验允许了 "a@b"，不够严格 → T-009)
```

### 字段约定

| 字段 | 必须 | 说明 |
|---|---|---|
| `id` | ✓ | `T-NNN` 单调递增，不重用 |
| `type` | ✓ | `dev` / `qa` / `design-update` / `manual` |
| `status` | ✓ | `pending` / `doing` / `ready_for_qa` / `done` |
| `title` | ✓ | 标题行 `###` 下的第一行自由文本 |
| `dependencies` | | 阻塞的前置 item id |
| `source` | | 衍生关系，如 `follows T-003` 或 `qa-finding of T-003` |
| `dev_notes` | | Dev 完成时追加的简述（导航用） |
| `findings` | | QA 的检查结论 |
| `attempt_log` | | dev 失败/成功历史（见 §7） |

### 为什么选 Markdown

1. LLM 生成 Markdown 比 JSON 稳定（见 memory "LLM JSON 解析陷阱"）
2. 人类可直接浏览
3. agent-park 现有 FileContentView 零改造即可展示
4. 解析只需识别 `### T-NNN · type:X · status:Y` 一行，其余自由文本

---

## 5. 状态机

### 合法转移

```
                   ┌─(dev 执行中)─┐
                   ▼              │
  pending ──(dev)──▶ doing ──(完成)──▶ ready_for_qa ──(qa 通过)──▶ done
                       │                      │
                       │                      └(qa 挑问题)
                       └(dev 失败)                   │
                       ▼                            │
                  pending                           │
                  + attempt_log++                   ▼
                                              追加新 T-NNN
                                              原 item 回 pending
```

### 状态语义

| 状态 | 含义 |
|---|---|
| `pending` | 等待被指派。可能是首次，也可能是 dev 失败后重排队、或 qa 退回 |
| `doing` | dev 正在执行（loop 运行时 agent 独占该 item） |
| `ready_for_qa` | dev 自认为完成，等 qa 检查 |
| `done` | qa 通过，不可再改 |

**注意**：没有 `blocked` 状态。dev 失败不停止循环，只是带 `attempt_log` 回 `pending`。

### 权限矩阵（loop validator 硬约束）

| actor | 允许转移 | 禁止 |
|---|---|---|
| `planner` | 创建任意 item（首次） | 不能运行第二次 |
| `dev` | 自己的 item: `pending→doing`, `doing→ready_for_qa`, `doing→pending`, `pending→ready_for_qa`（一次写入完成）, `pending→pending`（放弃） | 改 `done` item, 改他人 item, 改 design.md |
| `qa` | 被检查 item: `ready_for_qa→done/pending`；自己 qa item: `pending→done`；追加新 item | 改 `done` item 的 status, 改 design.md |

**违反 → loop 回滚 todolist 到 before，该轮作废，计入 attempt_log，下轮重新决策。代码改动不回滚（已提交到工作目录，只能由后续 cycle 或人类清理）。**

---

## 6. 调度循环

### 伪代码

```python
def run(design_path: Path) -> ExitCode:
    cwd = design_path.parent
    state = LoopState.load_or_init(cwd)
    config = AgentConfig.load(cwd)

    if not (cwd / "todolist.md").exists():
        run_agent("planner", cwd, None, config.planner)
        if config.review_plan:
            input("Press Enter to start loop...")

    while True:
        items = todolist.parse(cwd)

        if reason := state.should_exit(config.limits):
            return exhausted(reason)

        decision = pm.decide(items) if config.pm.is_code else llm_pm(items, config.pm)
        state.record_decision(decision)

        if decision.next == "done":
            return success()
        if state.same_decision_count >= 3:
            return exhausted("PM stuck (3 consecutive same decisions)")

        before = items
        run_agent(decision.next, cwd, decision.item_id, getattr(config, decision.next))

        after = todolist.parse(cwd)
        try:
            validator.validate_transition(before, after, decision.next, decision.item_id)
        except ValidationError as e:
            rollback(cwd, before)
            state.record_rollback(e)
            # 下一轮继续

        state.cycle += 1
        state.save(cwd)
```

### 退出条件（唯一）

触发下列任一 → `exhausted`：

| 上限 | 默认 | 含义 |
|---|---|---|
| `MAX_CYCLES` | 30 | PM 被调用的总次数 |
| `MAX_ITEM_ATTEMPTS` | 5 | 单个 item 被 dev 尝试的次数 |
| `MAX_COST_CNY` | 1000 | 总成本上限（cco/ccs stream-json 里的 `total_cost_cny` 累加） |
| PM 三连同决策 | — | 防死循环兜底 |

命令行参数可覆盖：
```
agentloop run design.md --max-cycles 50 --max-cost 2000
```

---

## 7. Dev 自重试 与 attempt_log

### 机制

Dev 失败后 item 回到 `pending`，下次 PM 仍会再次指派它。Dev 第 N 次拿到这个 item 时，prompt 里会带上之前失败的 `attempt_log`，让它能"看到自己之前试过什么"。

### attempt_log 保留策略

**只保留首次失败 + 最近 2 次**（首次最有信号价值，防止 prompt 膨胀）。

```markdown
- attempt_log:
  - cycle 2: pending (qa findings: 根本没处理空指针)    # 首次
  - cycle 15: pending (qa findings: 修了空指针但遗漏空字符串)  # 倒数第二
  - cycle 22: pending (qa findings: 覆盖了空字符串但 regex 错误) # 最近
```

### 触发 exhausted

单 item `attempt_log.length >= MAX_ITEM_ATTEMPTS` → loop 退出。用户看 `.agentloop/state.json` 知道哪个 item 卡住，可手动处理（改 design / 拆分 item / 放弃）。

---

## 8. 配置：每个 agent 独立后端

### project 级（优先）：`.agentloop/config.toml`

```toml
[limits]
max_cycles = 30
max_item_attempts = 5
max_cost_cny = 1000

[agents.planner]
cmd = "cco"
model = "claude-sonnet-4-6"      # 可选
timeout_sec = 600

[agents.dev]
cmd = "cco"

[agents.qa]
cmd = "ccs"                       # 只读判断，便宜模型

[agents.pm]
# 省略 cmd = 使用代码版 PM（首版默认）
# cmd = "ccs"                     # 未来切 LLM 版时放开
```

### 全局 fallback：`~/.agentloop/config.toml`

同 schema，project 级覆盖全局级。

### 内置默认值

planner / dev: `cco`；qa: `ccs`；pm: 代码版。

---

## 9. 命令行接口

```
agentloop run design.md                    # 首次运行或续跑
agentloop run design.md --fresh            # 删除 .agentloop/ 从零开始
agentloop run design.md --review-plan      # planner 后暂停等回车
agentloop run design.md --max-cycles 50    # 覆盖 cycle 上限
agentloop run design.md --max-cost 2000    # 覆盖成本上限

agentloop resume --more-cycles 20          # 已 exhausted 后追加预算继续
agentloop status                           # 显示当前 project 进度
```

### 恢复语义

- `agentloop run design.md` 再次运行 = 继续上次进度（跳过 planner，直接进循环）
- 已 exhausted 的 project 直接再 run 会立即再 exhausted → 必须 `resume --more-cycles N` 突破原上限

---

## 10. agent-park 集成

### 原则

**不绑定**。loop 是独立 CLI，agent-park 作为可选的只读 UI：

- loop 运行时不要求 agent-park 服务启动
- agent-park 不能干预 loop 执行
- agent-park 新增 "Project Watch" 面板（后续 M4 做），读 `.agentloop/runs/` 和 `todolist.md` 展示进度

### 路径共存

| 场景 | 使用 |
|---|---|
| 日常开发、随便一个项目 | `agentloop run ~/myproj/design.md` 独立跑 |
| 想看漂亮 UI | 打开 agent-park，"Project Watch" 面板指向 cwd |

---

## 11. 目录结构

### agent-park 仓库内

```
agent-park/
├── agentloop/                      ← 内嵌子项目
│   ├── README.md                   # 用户文档
│   ├── DESIGN.md                   # 本文档
│   ├── cli.py                      # 入口
│   ├── loop.py                     # 调度循环
│   ├── state.py                    # LoopState
│   ├── config.py                   # AgentConfig 加载
│   ├── todolist.py                 # todolist 解析/写入
│   ├── validator.py                # 状态机校验
│   ├── prompts/
│   │   ├── planner.md
│   │   ├── dev.md
│   │   ├── qa.md
│   │   └── pm.md                   # 代码版 PM 不用；LLM 版才用
│   ├── agents/
│   │   ├── base.py                 # run_agent 抽象
│   │   ├── planner.py
│   │   ├── dev.py
│   │   ├── qa.py
│   │   └── pm.py                   # 代码版 PM
│   └── tests/
│       └── test_state_machine.py
└── ... (其余 agent-park 代码不动)
```

### 工作目录（用户侧）

```
my-project/
├── design.md              ← 用户提供
├── todolist.md            ← planner 生成，dev/qa 维护
├── src/                   ← 项目代码
└── .agentloop/            ← 加入 .gitignore
    ├── config.toml
    ├── state.json
    └── runs/
        ├── 001-planner.jsonl
        ├── 002-dev-T001.jsonl
        ├── 003-qa-T002.jsonl
        └── ...
```

---

## 12. 关键模块接口（签名，非实现）

```python
# agentloop/state.py
@dataclass
class LoopState:
    cycle: int
    total_cost_cny: float
    last_decision: Decision | None
    same_decision_count: int
    started_at: str

    @classmethod
    def load_or_init(cls, cwd: Path) -> "LoopState": ...
    def save(self, cwd: Path) -> None: ...
    def should_exit(self, limits: Limits) -> str | None: ...
    def record_decision(self, d: Decision) -> None: ...


# agentloop/todolist.py
@dataclass
class Attempt:
    cycle: int
    result: str           # "ready_for_qa" | "pending"
    notes: str

@dataclass
class Item:
    id: str
    type: str
    status: str
    title: str
    dependencies: list[str]
    source: str | None
    dev_notes: str | None
    findings: str | None
    attempt_log: list[Attempt]

def parse(cwd: Path) -> list[Item]: ...
def write(cwd: Path, items: list[Item]) -> None: ...


# agentloop/validator.py
class ValidationError(Exception): ...

def validate_transition(
    before: list[Item],
    after: list[Item],
    actor: str,
    item_id: str | None,
) -> None: ...


# agentloop/agents/base.py
@dataclass
class Decision:
    next: str             # "dev" | "qa" | "done"
    item_id: str | None
    reason: str

@dataclass
class AgentBackend:
    cmd: str              # "cco" | "ccs" | ...
    model: str | None
    timeout_sec: int

@dataclass
class RunResult:
    stream_json_path: Path
    duration_sec: float
    cost_cny: float
    success: bool

def run_agent(
    role: str,
    cwd: Path,
    item_id: str | None,
    backend: AgentBackend,
) -> RunResult: ...


# agentloop/agents/pm.py
def decide(items: list[Item]) -> Decision:
    """代码版 PM：按规则表决策，不调用 LLM"""
    ...
```

### 代码版 PM 决策规则（严格顺序）

```python
def decide(items):
    # 1. 有 ready_for_qa → 派 qa
    for it in items:
        if it.status == "ready_for_qa" and it.type == "dev":
            qa_item = find_qa_for(it, items)  # 匹配 source: follows {it.id}
            return Decision("qa", qa_item.id, ...)

    # 2. 有 pending 且依赖都 done → 派 dev
    for it in items:
        if it.status == "pending" and it.type == "dev":
            if all(deps_done(it, items)):
                return Decision("dev", it.id, ...)

    # 3. 全是 done → 结束
    if all(it.status == "done" for it in items):
        return Decision("done", None, ...)

    # 4. 其他情况（比如只剩未匹配的 qa / 孤立 item）
    return Decision("done", None, reason="no actionable items")
```

---

## 13. 反模式清单

| 反模式 | 为什么禁止 |
|---|---|
| ❌ PM 写 todolist | 权责混乱，PM 只决策不修改 |
| ❌ Dev 写 findings | findings 只属于 QA |
| ❌ QA 改已 done 的 item | 违反不可变原则，发现新问题必须追加新 item |
| ❌ planner 中途再跑 | 规划只发生一次，中途重新规划 = 人类介入 |
| ❌ agent 间传递对话历史 | 必须通过文件契约通信 |
| ❌ loop 做智能判断 | loop 是状态机，智能都在 agent prompt |
| ❌ 把 design 做成可动文档 | design 是北极星，动 design 必须人类拍板 |
| ❌ 单 project 内并发执行多 dev | 保持顺序，简单可预测 |

---

## 14. 实现路线图（MVP）

| 里程碑 | 内容 | 交付 |
|---|---|---|
| **M0** | schema 定稿 | `DESIGN.md`（本文档）+ `README.md` 用户视角示例 |
| **M1** | 状态机核心 + 代码版 PM + 假 dev/qa（echo + 随机通过）+ 单测 | `test_state_machine.py` 覆盖状态转移、回滚、exhausted |
| **M2** | planner/dev/qa 接真 cco/ccs；config 加载 | 跑通一个最简 demo 项目 |
| **M3** | attempt_log 完整实现 + 回滚 + 退出兜底 | 真项目稳定运行 |
| **M4** | agent-park "Project Watch" 面板（只读） | 浏览器可见进度 |

---

## 15. 未来扩展（不在 MVP 内）

- **LLM 版 PM**：当规则不够用（如需"软判断"）时，配置 `agents.pm.cmd = "ccs"` 切换
- **并发 dev**：独立 item 可并发（需文件级锁），提升吞吐
- **design diff 传播**：design.md 被人工修改后，通知 loop 重算 todolist（目前需 `--fresh`）
- **复用 agent-park 的 memory/knowledge**：把过往项目的经验注入到 dev/qa 的 prompt
- **Ticket 系统接入**：`design.md` 从 Linear/Jira 自动拉取
