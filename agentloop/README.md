# agentloop

一个 CLI 脚本：给一个 `design.md`，自动调度 planner / PM / dev / qa 四个角色，跑到所有任务完成。

- 📘 **[快速上手 →](docs/quickstart.md)**（5 分钟跑通第一个项目）
- 🧠 详细设计见 [`DESIGN.md`](DESIGN.md)

---

## 能做什么

把"一份 design.md + 一行命令"变成"完整交付物"，无人值守：

| 角色 | 做什么 | 调用频次 |
|---|---|---|
| **planner** | 读 design，拆任务，写初始 todolist.md | 整个项目 1 次 |
| **PM** | 每轮看 todolist，决定下一步派谁（代码版，不走 LLM） | 每轮 1 次 |
| **dev** | 拿到一个 item，写代码，标记为 `ready_for_qa` | 每次 1 个 item |
| **qa** | 审查 dev 产物，判 `done` 或打回 `pending`（附 findings） | 每次 1 个 item |

核心特性：

- 🧩 **文件契约通信**：角色之间只通过 `todolist.md` 传递信息（context quarantine），不共享对话历史
- 🔒 **design.md 全程只读**：agent 改不了目标，防止需求飘移；真要改 design 必须人类介入
- 💾 **状态全落盘**：随时 Ctrl-C 随时恢复，`.agentloop/state.json` + `todolist.md` 是唯一真相源
- 🔁 **失败自重试**：dev 被 qa 打回会带着 `attempt_log` 再试，`max_item_attempts` 上限兜底
- 💰 **成本上限**：`max_cost_cny` 超了自动退出；每轮从 cco/ccs 的 stream-json 里抽 `total_cost_usd` 累加
- 🎛 **每个角色独立后端**：qa 可以配 ccs（便宜），dev/planner 配 cco（能打），PM 用代码版完全不花钱
- 🧱 **与 agent-park 解耦**：独立 CLI，不依赖 agent-park 服务；agent-park 未来可做只读 Project Watch 面板

---

## 快速开始

详细教程见 [`docs/quickstart.md`](docs/quickstart.md)。最小示例：

### 安装

```bash
# 从仓库根目录
cd /data1/common/agent-park
# agentloop 是一个可直接 `python -m` 运行的包，无需额外安装
```

### 第一次跑

```bash
mkdir ~/myproj && cd ~/myproj
# 写好 design.md
vim design.md

# 启动 loop（使用默认后端：planner/dev=cco, qa=ccs, pm=代码版）
python -m agentloop run design.md
```

- 首次会先跑 planner 生成 `todolist.md`
- 然后进入调度循环：PM 决策 → dev/qa 执行 → 验证 → 下一轮
- `.agentloop/` 目录保存状态和每轮日志，可中断可恢复

### 查看进度

```bash
python -m agentloop status design.md
```

### 预算超了要继续

```bash
python -m agentloop resume design.md --more-cycles 20
```

---

## 命令行参数

```
python -m agentloop run design.md                       # 跑或续跑
python -m agentloop run design.md --fresh               # 清 state/runs/todolist（保留 config.toml）
python -m agentloop run design.md --review-plan         # 强制开启计划确认闸门
python -m agentloop run design.md --max-cycles 50       # 覆盖 cycle 上限
python -m agentloop run design.md --max-cost 2000       # 覆盖成本上限（CNY）
python -m agentloop run design.md -v                    # 详细日志

python -m agentloop resume design.md --more-cycles 20   # 已 exhausted，追加预算
python -m agentloop status design.md                    # 进度表

python -m agentloop approve --workspace <slug>          # 批准计划，之后 run 即开始执行
python -m agentloop reject  --workspace <slug> --note "T-001 太大"
```

---

## 计划确认闸门（默认开启）

planner 产出 `todolist.md` 后，loop **不会**直接开跑，而是写下 `plan-review.json`
并以 `AWAITING_REVIEW` 退出，等人确认。这不是阻塞等待——进程干净退出，状态全在
workspace 里，几小时后再批准也没问题。

```
planner → todolist.md → [人工确认] → phase 1 (dev/qa 循环)
```

两种批准入口：agent-park UI 的批准按钮、CLI `agentloop approve`。飞书卡片只做
通知——它把待审的计划推给评审人并引导其到 UI 操作，卡片本身没有批准动作。

**审阅时该看什么**：不是 item 标题（它们看起来永远合理），而是**机器检查覆盖率**
——哪些 dev item 没有任何检查能验证它。UI 与飞书卡片都会把这一项标红。

**改计划比重新规划便宜**：直接编辑 `todolist.md` 再批准即可，批准时绑定的是编辑后
的内容（`todolist_digest`）。批准之后文件再被改动 → 闸门自动退回 `awaiting`，
需要重新批准，避免执行一份没人看过的计划。

策略配置：

```toml
[review]
plan = "always"          # 默认；每次规划后都要人工确认
# plan = "never"          # 关闭闸门（等同旧行为）
# plan = "when_unverified"  # 仅当存在"无机器检查覆盖"的 item 时才拦
```

---

## 可选配置

在项目 cwd 下创建 `.agentloop/config.toml`：

```toml
[limits]
max_cycles = 60
max_item_attempts = 5
max_cost_cny = 1000

[agents.planner]
cmd = "cco"
# model = "claude-sonnet-4-6"    # 可选
timeout_sec = 1800

[agents.dev]
cmd = "cco"

[agents.qa]
cmd = "ccs"

[agents.pm]
# 省略 cmd = 使用代码版 PM（首版默认）
```

全局 fallback：`~/.agentloop/config.toml`（相同 schema，项目级覆盖全局级）。

---

## 目录结构

```
my-project/
├── design.md              ← 你提供，全程只读
├── todolist.md            ← planner 生成，dev/qa 维护
├── src/                   ← 你的代码
└── .agentloop/            ← 加入 .gitignore
    ├── config.toml        ← 可选，agent 后端覆盖
    ├── state.json         ← loop 状态（cycle/cost/last_decision）
    └── runs/              ← 每轮 stream-json 日志
        ├── 001-planner.jsonl
        ├── 002-dev-T-001.jsonl
        └── ...
```

---

## 运行测试

```bash
cd /data1/common/agent-park
python3 -m pytest agentloop/tests/ -v
```

---

## 卡住了怎么办？

查看 `status` 输出、看 `.agentloop/runs/` 里最近一轮的 `.jsonl`、再决定：

1. 改 `design.md`，`--fresh` 重跑
2. 手动编辑 `todolist.md`（合并冲突、拆分 item），再 `resume`
3. 放弃当前分支
