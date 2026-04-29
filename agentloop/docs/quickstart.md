# agentloop 快速上手

5 分钟把一个 `design.md` 驱动的项目跑起来。

---

## 0. 你需要什么

- Python 3.10+
- 可执行的 `cco` 和 `ccs`（claude CLI 的 alias，分别走 Opus 和 Haiku/Sonnet）
- 一份描述你想做什么的 `design.md`

不需要：

- agent-park 服务（agentloop 是独立 CLI，不依赖服务端）
- 额外安装步骤（仓库内 `python -m agentloop` 直接可用）

---

## 1. 最小例子

```bash
# 建一个测试目录
mkdir ~/hello-proj && cd ~/hello-proj

# 写一份 design.md（越小越省钱）
cat > design.md <<'EOF'
# Hello 项目

## 目标
创建 `hello.py`，提供纯函数 `greet(name: str) -> str`：
- 返回 `"Hello, {name}!"`
- name 为空串或 None 时抛 ValueError

## 交付物
单文件 `hello.py`，不超过 20 行。
EOF

# 启动 loop
cd /data1/common/agent-park
python -m agentloop run ~/hello-proj/design.md -v
```

**预期**：

- 首轮 planner 生成 `todolist.md`（1 个 dev + 1 个 qa item）
- 第 1 轮 PM 派 dev → 生成 `hello.py` → todolist 更新为 `ready_for_qa`
- 第 2 轮 PM 派 qa → qa 通过 → 两个 item 都 `done`
- 第 3 轮 PM 返回 `done` → 退出 `SUCCESS`
- 成本约 ¥3（默认后端：planner/dev=cco，qa=ccs）

结束后你的目录长这样：

```
~/hello-proj/
├── design.md          ← 你写的，全程只读
├── hello.py           ← dev 产出
├── todolist.md        ← planner 生成，dev/qa 维护
└── .agentloop/
    ├── state.json     ← 进度（cycle、cost、rollback 记录）
    └── runs/          ← 每轮 LLM 的 stream-json 原始日志
        ├── 001-planner.jsonl
        ├── 002-dev-T-001.jsonl
        └── 003-qa-T-002.jsonl
```

---

## 2. 控制成本

默认后端对小项目可能偏贵。在项目目录新建 `.agentloop/config.toml`：

```toml
[limits]
max_cycles = 10
max_item_attempts = 3
max_cost_cny = 5.0

[agents.planner]
cmd = "ccs"            # 用便宜模型
timeout_sec = 300

[agents.dev]
cmd = "ccs"            # 小 case 用 ccs 就够了；复杂项目再换 cco

[agents.qa]
cmd = "ccs"            # QA 是只读判断，一直用 ccs
```

每个角色可以独立配后端。建议：

| 角色 | 小项目 | 正经项目 |
|---|---|---|
| planner | `ccs` | `cco`（一次性，想清楚） |
| dev | `ccs` | `cco`（实现质量影响最大） |
| qa | `ccs` | `ccs`（只读判断，便宜够用） |
| pm | 代码版（默认） | 代码版（不走 LLM） |

全局 fallback：`~/.agentloop/config.toml`，项目级覆盖全局级。

---

## 3. 三个常用命令

```bash
# 跑或续跑（.agentloop/ 存在就接着跑）
python -m agentloop run design.md

# 看进度
python -m agentloop status design.md

# 预算烧完了想继续
python -m agentloop resume design.md --more-cycles 20
```

`status` 的典型输出：

```
project:   /home/me/hello-proj
cycles:    2
cost:      ¥3.40
rollbacks: 0
last decision: done  — all items done

items:
  T-001   dev   done   创建 hello.py …
  T-002   qa    done   检查 T-001 …
```

---

## 4. 写一份好的 design.md

agentloop 的能力上限由 design.md 决定。几条实战经验：

1. **明确交付物**：写清楚"单文件 X"、"N 个函数"、"不超过 M 行"等可验证的硬指标，QA 才有判据
2. **列出非目标**：避免 dev 发散（"不需要 CLI"、"不需要测试"）
3. **尽量小**：先用小 case 跑通流程，再上真实项目。第一次别写 20 个任务的 design，先从 1-2 个任务起步
4. **别在 design 里埋模糊要求**：design 全程只读，dev 对着它抠细节，含混的要求会让 qa 不断打回 → 触发 `max_item_attempts` 退出

反面教材：

```markdown
# 聊天应用
做一个聊天应用，要好用、性能好、有现代感。   ← 全都没法验证
```

正面教材：

```markdown
# 聊天路由骨架

## 目标
在 server/routes_chat.py 新增 POST /chat 路由：
- 接收 {message: str}，长度 ≤ 500
- 返回 {reply: str}，内容固定为 "echo: {message}"
- 超长或缺字段返回 422

## 非目标
- 不接 LLM，只做 echo
- 不做鉴权、不做持久化
```

---

## 5. 常见问题

**Q：运行到一半我 Ctrl-C 了，怎么办？**
A：再跑一次 `python -m agentloop run design.md` 就接着上次的 cycle 继续。`.agentloop/state.json` 和 `todolist.md` 是唯一真相源。

**Q：报错 `EXHAUSTED: PM stuck on (...) — 3 consecutive identical decisions`？**
A：说明 dev 连续三轮没能推进某个 item。打开 `.agentloop/runs/` 看最近几轮的 jsonl，找出 dev 为什么卡住：通常是 design.md 要求太模糊、或 dev 被 QA 反复打回同一个点。改 design 然后 `--fresh` 重跑，或手动编辑 todolist 后 `resume`。

**Q：报错 `item T-xxx exceeded max_item_attempts`？**
A：该 item 失败超过上限（默认 5 次）。同上处理。

**Q：我改了 design.md，loop 怎么感知？**
A：不会感知。design 改动必须 `--fresh` 重跑（planner 重新规划）。这是故意的——design 是北极星，半路改容易乱。

**Q：`--fresh` 会删我的 config.toml 吗？**
A：不会。`--fresh` 只清 `state.json`、`runs/`、`todolist.md`，保留 `.agentloop/config.toml`。

**Q：能不能让 agentloop 并发做多个 item？**
A：当前版本不支持。单项目顺序执行（planner → PM → dev → qa → PM …）。多项目并发：开多个终端，每个终端跑自己的 `agentloop run`。

**Q：PM 用不用 LLM？**
A：默认不用。PM 是代码版，按 `DESIGN.md §12` 的决策表走：有 ready_for_qa 派 qa、有 pending 派 dev、全 done 就退出。如果你想切 LLM 版，在 config.toml 里加 `[agents.pm] cmd = "ccs"`（目前还没实现 LLM PM，保留位置）。

---

## 6. 下一步

- 读 [`DESIGN.md`](../DESIGN.md) 了解状态机、权限矩阵、回滚语义
- 读 `prompts/planner.md` / `dev.md` / `qa.md` 了解每个角色的硬约束——如果 agent 产出异常，八成是 prompt 和 design 冲突
- 跑 `python3 -m pytest agentloop/tests/ -v` 看单测覆盖的场景
