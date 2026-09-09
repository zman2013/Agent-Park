# Codebase Overview

> 阅读本文档可快速了解项目结构，无需每次重新探索代码库。

## 技术栈

- **后端**：Python FastAPI + WebSocket，`server/` 目录
- **前端**：Vue 3 + Pinia + Vite + Tailwind CSS，`frontend/` 目录
- **数据持久化**：JSON 文件（`data/` 目录），无数据库
- **Agent 执行**：`os.fork()` + PTY + `cco`/`ccs` 命令行工具（stream-json 协议）

## 目录结构

```
agent-park/
├── config.json              # 全局配置（端口、Agent 列表、memory、automemory、knowledge）
├── run.sh                   # 启动/停止/重启脚本
├── requirements.txt
├── pyproject.toml
├── server/                  # Python 后端
│   ├── main.py              # FastAPI 入口，lifespan 启动定时任务，挂载路由
│   ├── models.py            # 数据模型（Agent、Task、Message、TaskStatus）
│   ├── state.py             # 内存状态管理 + JSON 持久化（AppState）
│   ├── agent_runner.py      # 核心：子进程管理、PTY、流式输出处理
│   ├── routes_ws.py         # WebSocket 路由，消息分发，broadcast()，定时任务
│   ├── routes_rest.py       # REST API（Agent/Task CRUD、Memory、Knowledge）
│   ├── auto_memory.py       # 四层记忆：effective_id、build_context、consolidate + 四道闸门
│   ├── helper_llm.py        # 辅助 LLM 调用的共用管道（只读 settings + stream-json 解析）
│   ├── profile_store.py     # profile.md ↔ 旧 note 形状的 REST 转接层
│   ├── knowledge.py         # 热点文件统计 + project 信号提取 + 只读知识归档
│   ├── task_notify.py       # Task 终态飞书通知（卡片拼装 + 发送）
│   └── config.py            # 读取 config.json 配置
├── frontend/
│   └── src/
│       ├── App.vue                           # 根组件（可拖拽左右布局）
│       ├── main.js                           # Vue 应用入口，挂载 Pinia
│       ├── stores/agentStore.js              # Pinia 全局状态
│       ├── composables/useWebSocket.js       # WebSocket 连接与消息处理
│       └── components/
│           ├── AgentTree.vue                 # 左侧 Agent 树形列表
│           ├── AgentGroup.vue                # Agent 分组面板（含🧠知识总结按钮）
│           ├── ChatView.vue                  # 聊天面板，流式消息展示
│           ├── ChatInput.vue                 # 用户输入框
│           ├── TaskItem.vue                  # 任务列表项
│           ├── MessageBubble.vue             # 消息气泡（Markdown + 代码高亮）
│           ├── MemoryPanel.vue               # Agent 记忆面板（含知识标签页）
│           ├── TerminalPanel.vue             # 终端面板
│           ├── FileContentView.vue           # 文件内容预览
│           ├── FileBrowserPanel.vue          # 文件浏览器
│           ├── FileBrowserNode.vue           # 文件树节点
│           ├── UnseenTasksPanel.vue          # 未读任务指示
│           └── ToastContainer.vue            # 消息提示
├── scripts/
│   └── migrate_automemory.py  # 一次性迁移到四层文档（支持 --dry-run）
├── tests/                     # pytest（`python3 -m pytest tests/`，venv 内无 pytest）
├── data/
│   ├── agents.json          # Agent 元数据 + 排序顺序
│   ├── sessions.json        # cco 会话 ID（用于续话）
│   ├── tasks/               # 按 Agent 分离的任务文件
│   │   └── {agent_id}.json
│   ├── memory/              # Agent 记忆
│   │   ├── {eid}/            # 四层文档 profile/lessons/project/hotfiles.md
│   │   └── {eid}.jsonl       # 旧扁平格式，只读归档（已无代码读写，留作回滚依据）
│   └── knowledge/           # 只读归档（迁移前的历史产出，已无代码写入）
│       └── {effective_id}/  # errors.md / project.md / hotfiles.md
└── docs/                    # 项目文档
```

## 数据模型（server/models.py）

```python
class TaskStatus(str, Enum):
    idle = "idle"         # 初始/等待发送
    running = "running"   # 执行中
    waiting = "waiting"   # 等待用户输入
    success = "success"   # 成功完成
    failed = "failed"     # 执行失败

class Agent(BaseModel):
    id: str               # 12 字符 UID（由 name 哈希生成）
    name: str
    command: str = "cco"  # 执行命令
    cwd: str = ""         # 工作目录（空字符串 = 不切换）
    task_ids: list[str]
    shared_memory_agent_id: str | None = None
    pinned: bool = False

class Task(BaseModel):
    id: str
    agent_id: str
    name: str
    prompt: str
    status: TaskStatus
    messages: list[Message]
    num_turns: int
    total_input_tokens: int    # 累计输入 Token
    total_output_tokens: int   # 累计输出 Token
    context_window: int
    total_cost_cny: float      # 累计成本（人民币）
    model_usage: dict          # 按模型统计：{model_name: {inputTokens, outputTokens, ...}}
    updated_at: str            # ISO UTC 时间戳
    fork_session_id: str | None = None  # Fork 时记录源 session_id（一次性消费）

class Message(BaseModel):
    id: str
    role: str             # "user" | "agent"
    type: str             # "text" | "tool_use" | "tool_result" | "system"
    content: str
    tool_name: str = ""
    streaming: bool = False
```

## 任务执行流程（agent_runner.py）

### 入口：`run_task(task_id, prompt)`

```
run_task()
  └─ 设置 task.status = running
  └─ _start_subprocess()  →  在新线程中调用 _run_subprocess()

_run_subprocess()
  1. 获取 agent.cwd，校验路径存在性（不存在则报错返回）
  2. 检查 task.fork_session_id（fork 模式）或 session_id（resume 模式）
  3. 构建 cco 命令参数：
     - fork 模式：--resume <源sid> --fork-session <prompt>
     - resume 模式：--resume <sid> <prompt>
     - 新会话模式：<prompt>
  4. os.fork() + pty.openpty() 启动子进程
  4. 子进程：os.chdir(cwd) → os.execvpe(cco, args, env)
  5. 父进程：异步读取 master_fd，逐行解析 stream-json
  6. 每行 JSON 交给 _handle_chunk() 处理
  7. 收到 result 块 → _finish_task(success/failed)
```

### cco stream-json 协议

| chunk type | 说明 |
|---|---|
| `system` / `subtype: init` | 初始化，携带 session_id |
| `stream_event` / `message_start` | 新消息开始 |
| `stream_event` / `content_block_delta` | 流式文字/工具调用增量 |
| `assistant` | 完整消息体（验证用） |
| `result` / `subtype: success\|error` | 任务结束 |

### 错误处理

- **工作目录不存在**：fork 前校验，发送系统消息，标记 failed（`agent_runner.py:146`）
- **会话过期**（`No conversation found`）：清除 session_id，提示用户重新发消息
- **命令不存在**（`FileNotFoundError`）：降级到 mock 模式
- **子进程异常退出**：非 0 退出码 → `TaskStatus.failed`
- **服务重启**：`state.py` 启动时将 running/waiting 状态重置为 failed

### Fork Task（会话分支）

从已有 Task 分叉出独立会话分支，继承完整消息历史：

```
用户点击 ⑂ Fork 按钮
  → WS: { type: "fork_task", task_id: "xxx" }
  → state.fork_task(): 创建新 Task，深拷贝消息，设置 fork_session_id
  → broadcast task_created → 前端自动切换到新 Task

用户在新 Task 中发送第一条消息
  → _run_subprocess() 检测 fork_session_id
  → 启动 cco --resume <源sid> --fork-session <prompt>
  → cco 返回新 session_id → 自动保存
  → 后续消息走正常 --resume <新sid> 流程
```

关键实现：
- `Task.fork_session_id`：一次性字段，记录待 fork 的源 session_id，首次发消息时消费
- `state.fork_task()`：深拷贝消息（新 id），名称加 "(fork)" 后缀
- `_run_subprocess()`：检测 `fork_session_id` 构建 `--fork-session` 命令参数
- Fork 模式不注入 memory（fork 会话已有完整上下文）
- Token/Cost 统计从 0 开始，不继承源 Task

## WebSocket 消息协议（routes_ws.py ↔ useWebSocket.js）

### 客户端 → 服务端

| type | 说明 |
|---|---|
| `create_task` | 创建新任务 |
| `user_message` | 发送消息（触发 run_task） |
| `fork_task` | Fork 一个已有任务（复制消息历史，创建独立会话分支） |
| `stop_task` | 中止任务 |
| `set_agent_order` | 重排序 Agent |
| `generate_summary` | 手动触发巩固（`agent_id`, `date_range: "today"\|"recent_n"`）；不受 `automemory.daily_enabled` 限制 |

### 服务端 → 客户端（broadcast）

| type | 说明 |
|---|---|
| `state_sync` | 连接建立时下发完整状态 |
| `task_created` | 任务创建完成 |
| `task_updated` | 任务状态变更 |
| `message` | 新消息 |
| `message_chunk` | 流式增量内容 |
| `message_done` | 消息流结束 |
| `agent_created` | Agent 创建完成 |
| `agents_reordered` | Agent 排序更新 |
| `summary_progress` | 巩固进度（`step`, `detail`） |
| `summary_done` | 巩固完成（`added`/`updated`/`deleted`/`refused` + `failed_layers`） |
| `summary_error` | 巩固失败（`error`） |

## Task 完成飞书通知（task_notify.py）

Task 到达终态时把该轮最后一条 Agent 消息推送到飞书。配置段 `task_notify.feishu_notify`（`server/config.py::task_notify_config()`），默认关闭。

### 调用链

```
agent_runner._finish_task(task_id, status)
  └─ _prepare_notify()      # 配置未开启则返回 None（避免无谓的深拷贝）
       ├─ 读 agent.name
       ├─ task.model_copy(deep=True)          # 在 await 前快照，防并发 resume 污染
       └─ _run_start_index[task_id]           # 本轮起始消息下标
  └─ _schedule_notify()     # asyncio.create_task，脱离可取消的子进程协程
       └─ task_notify.notify_task_finished(agent_name, task_snapshot, start_index)
            └─ wiki_notify.send_feishu_card()  # 复用同一套 feishu-bot CLI
```

### 关键约束

| 约束 | 实现 |
|---|---|
| 只发一次 | `_finish_task` 的 `was_terminal` 判断，已是终态则跳过 |
| 只取本轮消息 | `_run_start_index[task_id]`（run 启动时记录 `len(task.messages)`）；内部续接不追加 user 消息，故不能用 role 边界判断 |
| auto-compact 续接不误报 | `compact_will_continue` 检查 `_compact_pending`，续接完成后才通知 |
| 快照防污染 | 在首个 `await` 前 `model_copy(deep=True)`；并发 `send_input()` 会改写 status 与 `_run_start_index` |
| 通知不被 kill | `_schedule_notify` 用独立 task + `_notify_tasks` 强引用，resume 的 `cancel_existing=True` 不会中断发送 |
| 不超 ARG_MAX | 消息截断 4000 字符、名称 200 字符（CLI 的 `--max-len` 在 argv 解析后才生效，救不了 exec 本身） |
| 关服不丢通知 | `shutdown()` 用 `asyncio.wait(_notify_tasks, timeout=...)` 兜底等待。单次预算 `NOTIFY_DRAIN_BASE_SECONDS=40` 必须大于 CLI 自身的 30s 超时，否则 CLI 子进程来不及被回收。同一 task 的通知**合并而非排队**（`_inflight` + 单个 `_pending` 槽，新的覆盖旧的），故最坏只有 `MAX_SERIAL_SENDS=2` 次串行调用，预算恒为 40×2=80s，需小于 `run.sh` 的 95s 强杀宽限 —— 定值预算不会漏掉「已 schedule 但尚未开始」的协程，而按运行时深度采样会 |
| 通知不丢最新结果 | 合并时保留**后到**的卡（`_pending` 覆盖），不是拒绝新来者：若被丢的是最后一轮，飞书会永久停留在旧结果上 |

**配置缓存**：`server/config.py` 的 `_CONFIG` 是模块级单次加载，改 `config.json` 后必须重启服务才生效。

## 状态持久化（state.py）

- `AppState` 单例，内存中维护所有 agents 和 tasks
- `save_agents()` → 写入 `data/agents.json`（Agent 元数据 + 排序）
- `save_agent_tasks(agent_id)` → 写入 `data/tasks/{agent_id}.json`（按 Agent 分离）
- `save_sessions()` → 写入 `data/sessions.json`（cco 续话用）
- 启动时从 JSON 恢复，running/waiting 任务重置为 failed

## Agent 记忆管理（auto_memory.py）

### 四层文档

```
data/memory/{effective_id}/
├── profile.md      # L1 交互偏好/人工规则 —— 人写，巩固永不改
├── lessons.md      # L2 错误 → 正确做法 —— 纠正/失败派生
├── project.md      # L3 项目事实 —— 观察派生
└── hotfiles.md     # L4 文件热度 —— 纯统计，零 LLM
```

| 层 | 优先级 | 权威来源 | 字符上限 |
|---|---|---|---|
| profile | 1 | 人工 | 2,000 |
| lessons | 2 | 纠正/失败派生 | 8,000 |
| project | 3 | 观察派生 | 10,000 |
| hotfiles | 4（无权威） | 纯统计 | 4,000 |

**巩固永不写 `profile.md`** —— 它是唯一人工权威源，自动写入会破坏用户对系统的信任基础。

**这是唯一的记忆系统，没有开关、没有回退路径。** 旧的扁平 `{eid}.jsonl` 读写（`server/memory.py`）连同 `automemory.enabled` 开关一起删掉了 —— 留一个永远为 `true` 的开关，只是把两条代码路径的维护成本伪装成安全感。`automemory.daily_enabled` 只管无人值守的 00:30 循环，注入与手工 🧠 不受它限制。

`data/memory/*.jsonl` 八个文件**留在原地但已无任何代码读写**，与 `data/knowledge/` 同为只读归档。它们是回滚这次删除的唯一依据，所以别顺手清理。

`build_context` 对没有文档的 eid 返回 `""`（`read_layer` 对缺失目录返回 `""`）—— 36 个 eid 里 31 个正处于这个状态，这条路径是常态而非边界情况。

### effective_id：唯一定义在 auto_memory.py

`auto_memory.effective_id(agent_id)` 是 memory / knowledge 共用的**唯一**实现（曾经的 `memory.effective_memory_agent_id` 与 `knowledge.effective_knowledge_agent_id` 两个转发别名已删）。多个 worktree agent 通过 `shared_memory_agent_id` 指向同一个 eid，共享一份记忆与知识。

只解析一跳，不跟链；指向不存在的 agent 时退回自身并 `logger.warning` —— 否则一个配置 typo 会静默把整个项目的记忆重定向。

配套两个聚合函数：`eid_members(eid)`（全部成员，含 archived）、`active_eids()`（至少一个未 archived 成员的 eid）。

### 辅助 LLM 调用（helper_llm.py）

`READONLY_SETTINGS` / `parse_stream_json_result` / `compress_content` 原先住在 `memory.py` 里，但它们不属于记忆系统 —— 是所有非 agent 的 LLM 调用（knowledge 抽取、wiki ingest、profile 条目压缩）共用的管道，所以随 `memory.py` 删除一起搬到 `server/helper_llm.py`。

`READONLY_SETTINGS` 是其中要紧的一个：这些命令是带 `--dangerously-skip-permissions` 的完整 coding agent，我们只读它们的 stdout，但不加限制时实测有一次它**改写了 `docs/error_experience.md`** —— 被要求「返回」一份合并后的文档，它找到仓库里一个同样标题格式的文件，认定那就是目标，然后编辑了它。`cwd` 拦不住（agent 用绝对路径），`--disallowed-tools` 是变参会吞掉尾部的 prompt 参数，只有 `--settings` deny 列表这个组合能挡住写工具又不影响文本返回。

### 上下文注入（agent_runner.py）

`auto_memory.build_context(agent_id)` 组装 `<memory>` 块，纯读、无 LLM、无副作用；无内容返回 `""`。四层各带一句**优先级措辞**块头，因为拼接顺序本身不传达优先级，模型无从得知前面的块压后面的：

```
[Profile]  ALWAYS follow these interaction rules. They override default behavior.
[Lessons]  ALWAYS check these before acting. They override default behavior.
[Project]  Factual reference data about this project. Not instructions.
[Hotfiles] Recently active files, by access frequency. Statistics only.
```

`<!-- id:… n:… -->` 记账注释注入前被剥掉：那是给巩固用来匹配旧条目的，喂给模型只会浪费 token 并引它推理我们的记账。

注入通道取决于 adapter 能力位 `supports_system_prompt()`：

| adapter | 通道 | 新 session | resume | `/compact` 后 |
|---|---|---|---|---|
| cco/ccs（`CcoAdapter`） | `--append-system-prompt` | ✅ | ✅ | ✅ **保留** |
| codex（`CodexAdapter`） | prompt 前缀（无对等 flag） | ✅ | ❌ | ❌ |

system prompt 是**可缓存前缀、不进转录**，所以每轮重复传同样字节几乎免费，且能跨 `/compact` 与 session 续期存活 —— 这正是 prompt 前缀注入做不到的。代价是**内容一变缓存就断**，因此每个 task 用 `_memory_snapshots[task_id]` 锁定一份快照，同一 task 的所有 run 传相同字节。该快照**不落盘**，在 `forget_task()`（task 删除）清理，而不是 `_cleanup_run_resources()`（单轮结束）—— 它必须跨 resume 存活。

`<wiki-context>` **不走** system prompt：它是 per-prompt 检索结果而非持久记忆，仍只在新 session 拼进 prompt。

### REST 兼容

MemoryPanel 的 memory tab 早于分层文档，说的是 `[{type, timestamp, content, line_index}]`。它一直在编辑的就是 profile 层，所以 `server/profile_store.py` 做一层形状转换（bullet ↔ note，日期存为行尾注释），整个 tab 无需改动。

两处不对等，都是刻意的：

- **多行条目**：磁盘格式是「一个条目一个 bullet」，所以第 2 行起用两空格续行缩进。`POST /memory` 接受用户自由输入（上限 300 字符），两行规则很正常 —— 没有续行约定的话第 1 行之后会被静默丢掉，**连带行尾的日期注释**。条目**内部的空行不保留**：那需要输出只含缩进的行，而多数编辑器保存时会剥掉行尾空白，profile.md 是给人手改的，这个格式没法诚实地承诺往返。`tests/test_profile_store.py` 钉住这条契约。
- **时间戳精度**：旧的是 ISO（`2026-04-07T03:22:11Z`），profile.md 只存日期。前端 `formatTs` 对 date-only 单独分支 —— 否则 `new Date('2026-04-07')` 按 UTC 午夜解析，东八区看到 `04/07 08:00`，一个从未记录过的时刻。

`scripts/migrate_automemory.py` 的 `render_profile` **复用 `profile_store._render`** 而不是自己拼一份：两者必须对续行约定取得一致，各写一份的话迁移会写出面板静默截断的 bullet。

### 已知缺口：6 个 eid 的 project 层需要重新派生

迁移只把 `note` 迁进 profile、`knowledge_summary` **全部丢弃**，所以只有含 `note` 的 eid 才有内容。删除旧系统前实测 36 个 eid 里 8 个有注入内容，新旧对照：

| eid | agent | 近30d 活跃日 | 旧 → 新 |
|---|---|---|---|
| `1b158839f8aa` | schumacher-compiler-ci | 0 | 1377c → **0** |
| `2876150ba8c6` | feishu-bot | 1 | 824c → **0** |
| `440f3041c89a` | fm | 0 | 1291c → **0** |
| `ce7611e7481e` | talk | 0 | 2078c → 243c |
| `867aac932032` | ccgs | 0 | 915c → 244c |
| `41dd70bc1d00` | claude-code-router | 0 | 1526c → 242c |
| `f4bfb91dfc93` | agent-park | 4 | 2590c → **6160c** |
| `648e67d1ac10` | compiler | 11 | 1110c → **1232c** |

**决定是接受这段真空，不补迁**（备选是把 `knowledge_summary` 剥掉 `。详见 …md` 尾巴后作为 project 种子）。依据是活跃度分布：六个受影响的 eid 近 30 天合计只有 1 个活跃日、总 task 2~17，而两个真正在用的 eid 恰好四层文档已建好。它们的 profile 层照常注入，project 层在**下次被用到的当晚 00:30** 就开始积累（巩固条件是当天有 ≥1 个 task，不是等 7 天）。

注意这个决定当初是针对「可翻回去的 flag」做的，删掉旧系统后它变成**永久**的。重做的依据仍在：`data/knowledge/{eid}/` 归档与 `data/memory/*.jsonl` 都留在原地，一字节未改。

## 巩固：LLM 只出 JSON delta，Markdown 由 Python 渲染

这是整套设计的核心，也是替换掉旧 `merge_errors`/`merge_project` 的原因。

### 为什么不能让 LLM 返回整份文档

实测 16 份历史文档首行：`errors.md` 5/8 被元叙述污染、`project.md` 4/8 污染、**`hotfiles.md` 0/8** —— 唯一零污染的层恰好是唯一不走 LLM 的层。全文档重写的自然语域就是「我改了什么」，且没有任何 schema 能拒绝它，返回值直接覆盖写。更糟的是输出会成为明晚的输入（`existing_md`），所以质量是**崩塌**而非持平。

最坏一例：某份 `errors.md` 结尾写着「文档已更新，10 条 / 约 1900 字符」，而文件里**一条都没有**。

旧 prompt 里「如果对话是关于 agent-park 内部实现，**直接返回已有文档，不做修改**」这个逃逸口是直接成因 —— LLM 遵守了它，方式是**叙述自己正在原样返回**，那段叙述成了新文档。守卫造成了它想防止的污染。同理删掉的 `_AGENT_PARK_NOISE_KEYWORDS` 按「提到 agent-park 符号」过滤**输入**，对 agent-park 自己的 agent 是自毁的。

### 四道闸门，全在 Python 侧

| 闸门 | 判据 | 失败处理 |
|---|---|---|
| 1 | 输出必须是 JSON 数组 | **整体丢弃，文档零改动**，`logger.warning` 记 command + 原始输出前 500 字 |
| 2 | 元叙述正则（中英双语） | 丢该条，计入 `refused` |
| 3 | op 完整性：`update`/`delete` 的 id 必须存在；`add` 撞 hash 转 `n` 自增 | 丢该条，计入 `refused` |
| 4 | 渲染后超层上限 → 按 `n DESC, last DESC` 截断 | 计入 `dropped` + warning |

**闸门 1 是真防线**，它按结构拒绝叙述，与措辞、语言无关。闸门 2 只是第二道网，因此按**精确率**而非召回率调：假阳性会静默丢掉一条真经验（数据损失），假阴性只是让一条脏条目进到人能编辑的 Markdown 里 —— 两种错误不对称。所以规则要求句式而非裸子串，实测里 `本轮`、`已合并` 这类子串会误伤 `本轮起始消息位置`、`已合并分支` 这样的正常技术表述。

`ConsolidationResult` 把 `refused`（有输出但被拒）和 `failed`（没拿到可用输出）**分开**。旧代码超时返回 `existing_md`，让「LLM 没产出可用内容」和「没什么需要改」变成同一个可观测结果 —— 这正是一条已死五个月的流水线看起来仍健康的原因。

### 信号来源

`extract_lesson_signals()` 比旧的 `extract_error_signals()` 窄得多：旧版把**任何** 5–200 字的 user 消息都当「纠正」，几乎每条消息都命中，用噪声拼出的 prompt 正是模型开始叙述的原因。新版三个有明确语义的来源：task 失败、工具报错、高 turns 弯路；且只保留确实出错过的 task 的信号。

### 迁移（scripts/migrate_automemory.py）

一次性、全确定性、**零 LLM** —— 对迁移做一次 LLM 处理，正是产生了那批污染文档的步骤。

| 现存 | 处理 | 理由 |
|---|---|---|
| `note` × 22 | 迁进 `profile.md`，原文保留，日期存为行尾注释 | 全部是人工写的交互规则，正好就是 profile 层 |
| `knowledge_summary` × 64 | **全部丢弃** | 每条是 `<一条 bullet>。详见 …md` 的有损派生，且 16 份源文档中 9 份已污染 |
| `data/knowledge/{eid}/*.md` | 原地保留、只读归档、**不导入** | 8 份文档 8 种形状，且导入污染内容会立刻触发反馈放大 |
| `docs/error_experience.md` | 作为 agent-park eid 的 `lessons.md` 种子 | 41 行、10 条编号错误模式带出现次数、零污染、人工校验过 |

原 `{eid}.jsonl` **一个字节都不改**（md5 校验过），四层文档写进同级的 `{eid}/` 目录。重复运行会**拒绝覆盖**已存在的文档并以 exit 1 退出 —— 否则第二次迁移会盖掉巩固之后的产出。

## 知识文档归档（knowledge.py）

`data/knowledge/{eid}/` 保留为**只读归档**，供回滚；已无任何代码写入它。`knowledge.py` 剩下的部分：

- `compute_hotfiles()` / `build_hotfiles_md()` —— 纯 Python 文件热度统计，一个字不改地复用
- `extract_project_signals()` —— project 层的信号来源
- `_llm_call()` —— 只读的辅助 LLM 调用（`--settings` deny 列表 + `_clean_env()`），与 auto-memory 共用；deny 列表本身在 `helper_llm.py`
- `read_knowledge_docs()` —— 读归档


## 定时任务（routes_ws.py）

所有定时任务均用 asyncio 原生实现，无外部框架依赖。

| 任务 | 实现 | 触发时机 |
|------|------|----------|
| WebSocket 心跳 | `_heartbeat_loop()`，每 20 秒 broadcast ping | 首个 WS 客户端连接时 |
| 每日知识总结 | `_daily_summary_loop()`，每天 00:30（`DAILY_SUMMARY_HOUR/MINUTE`） | 应用启动时（lifespan） |
| 每日 wiki ingest | `_wiki_ingest_loop()`，默认 00:00（`wiki_ingest.schedule`） | 应用启动时（lifespan） |

知识总结定在 00:30 而非 00:00，是为了与 wiki ingest 错开，避免两套 LLM 调用在午夜串行堆积。

**每日巩固流程**：
1. 应用启动 → `lifespan` → `ensure_daily_summary_task()`（受 `automemory.daily_enabled` 控制）
2. 循环计算到下一个 00:30 的秒数，`asyncio.sleep()`
3. 醒来 → `run_daily_summary_all(date)` → 对 `active_eids()` 中每个 **eid** 执行 `_run_daily_summary(eid, date)`
4. 按 `task.updated_at` 过滤前一天的任务，无任务则跳过
5. 调用 `auto_memory.consolidate()`，经四道闸门后写入四层文档

> 这个循环曾经**从未启动过**：`main.py` 的门是 `knowledge.enabled`，而它是 `false`（日志里 109 条 "Daily knowledge summary is disabled by config"）。也就是说光把 `automemory.enabled` 翻成 `true` 什么都不会发生 —— 一个开关守着另一套配置的开关，是这类静默失效的典型形状。现在门与被门控的东西同属 `automemory`。

**为什么按 eid 而非 agent_id 遍历**：514 个 agent（480 已 archived）只对应 25 个活跃 eid。按 agent_id 遍历时，共享同一份文档的 N 个 agent 会让 LLM 在同一份文档上跑 N 遍（`648e67d1ac10` 有 478 个成员）；更糟的是 `compute_hotfiles()` 只吃单个 agent 的 task 后覆盖写，后写的赢 —— 实测按 eid 汇总得到 6991 个文件，按单 agent 只有 622 个。`_eid_tasks(eid)` 负责汇总，archived agent 的历史已冻结故 `active_eids()` 跳过。手工 🧠（`_run_generate_summary`）走同一条聚合路径。

## 前端状态管理（agentStore.js）

```javascript
// 核心状态
agents        // Agent 列表（含 pinned 排序）
tasks         // 任务 Map：{ task_id: Task }
currentTaskId // 当前选中任务
collapsed     // 折叠状态
unseenTaskIds // 未读任务
memoryPanelOpen / memoryAgentId / agentMemory  // 记忆面板
```

- WebSocket 消息驱动状态更新，无需手动轮询
- 流式消息通过 `message_chunk` 增量更新 task.messages
- `summary_progress/done/error` 消息通过 CustomEvent 分发给 AgentGroup 和 MemoryPanel

## REST API 参考

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agents` | 列出所有 Agent |
| POST | `/api/agents` | 创建 Agent |
| PATCH | `/api/agents/{id}` | 更新 Agent |
| GET | `/api/agents/{id}/memory` | 读取 Agent 记忆 |
| POST | `/api/agents/{id}/memory` | 添加记忆条目（LLM 压缩） |
| DELETE | `/api/agents/{id}/memory/{idx}` | 删除记忆条目 |
| GET | `/api/agents/{id}/knowledge` | 读取四层文档（lessons/project/hotfiles/profile）+ 只读 archive |
| GET | `/api/agents/{id}/files` | 文件浏览 |
| GET | `/api/agents/{id}/files/content` | 读取文件内容 |
| POST | `/api/agents/{id}/tasks` | 创建任务 |
| DELETE | `/api/tasks/{id}` | 删除任务 |

## 常用操作参考

```bash
# 启动/重启服务
./run.sh start
./run.sh restart

# 查看日志
tail -f logs/backend.log
tail -f logs/frontend.log

# 数据文件
cat data/agents.json            # Agent 列表
cat data/sessions.json          # cco 会话 ID
cat data/tasks/{agent_id}.json  # 某 Agent 的所有任务
ls data/memory/                 # 各 eid 的四层记忆文档
```
