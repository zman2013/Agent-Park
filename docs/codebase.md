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
├── config.json              # 全局配置（端口、Agent 列表、memory、knowledge）
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
│   ├── memory.py            # Agent 记忆读写（JSONL 格式）
│   ├── knowledge.py         # 知识总结：信号提取、LLM 合并、文档写入、memory 索引
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
├── data/
│   ├── agents.json          # Agent 元数据 + 排序顺序
│   ├── sessions.json        # cco 会话 ID（用于续话）
│   ├── tasks/               # 按 Agent 分离的任务文件
│   │   └── {agent_id}.json
│   ├── memory/              # Agent 记忆（JSONL 格式）
│   │   └── {agent_id}.jsonl
│   └── knowledge/           # Agent 知识文档（每日 summary 产出）
│       └── {effective_id}/
│           ├── errors.md    # 错误经验
│           ├── project.md   # 项目知识
│           └── hotfiles.md  # 文件热度统计
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
| `generate_summary` | 手动触发知识总结（`agent_id`, `date_range: "today"\|"recent_n"`） |

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
| `summary_progress` | 知识总结进度（`step`, `detail`） |
| `summary_done` | 知识总结完成（`files_updated`, `memory_entries`） |
| `summary_error` | 知识总结失败（`error`） |

## 状态持久化（state.py）

- `AppState` 单例，内存中维护所有 agents 和 tasks
- `save_agents()` → 写入 `data/agents.json`（Agent 元数据 + 排序）
- `save_agent_tasks(agent_id)` → 写入 `data/tasks/{agent_id}.json`（按 Agent 分离）
- `save_sessions()` → 写入 `data/sessions.json`（cco 续话用）
- 启动时从 JSON 恢复，running/waiting 任务重置为 failed

## Agent 记忆管理（memory.py）

- 格式：JSONL，每行一条 `MemoryEntry`（timestamp、type、content）
- 文件位置：`data/memory/{agent_id}.jsonl`
- 支持多 Agent 共享记忆（通过 `shared_memory_agent_id`）
- 最大行数由 `config.json` 的 `memory.max_lines` 控制

## 知识总结系统（knowledge.py）

从 Task 对话历史中提炼可复用知识，沉淀为持久化文档，注入 Agent 记忆。

### 存储

```
data/knowledge/{effective_id}/
├── errors.md       # 错误经验（错误 → 正确做法，按频率排序）
├── project.md      # 项目知识（目录结构、常用命令、约定）
└── hotfiles.md     # 文件热度统计（最近 7 天读写频率 top 20）
```

`effective_id` 与 memory 共用同一套 `shared_memory_agent_id` 逻辑，同项目多 worktree agent 共享同一份知识。

### 提取流程

```
Step 1: 规则提取（无 LLM）
  - extract_error_signals()：tool_result 含 error/traceback + user 纠正消息
  - extract_project_signals()：agent text 含路径/命令描述 + user 告知的事实
  - compute_hotfiles()：统计 Read/Edit/Write tool_use 中的文件访问频率

Step 2: LLM 合并
  - 旧 errors.md + 新信号 → LLM → 新 errors.md（去重、合并计数、排序）
  - 旧 project.md + 新信号 → LLM → 新 project.md（同主题覆盖、保持分类）
  - hotfiles：纯计算，无需 LLM

Step 3: 写入
  - 覆盖写三个 .md 文件
  - 从文档内容逐条构建 memory 条目（每条知识独立一条）
  - 删除 memory 中旧的 knowledge_summary 条目，写入新条目
```

### Memory 注入格式

```
[错误经验] 错误简述。正确做法：...。详见 data/knowledge/{eid}/errors.md
[项目知识] 知识点内容。详见 data/knowledge/{eid}/project.md
[热点文件] 近期高频文件: file1(读N/改M), ...。详见 data/knowledge/{eid}/hotfiles.md
```

Agent 启动时通过现有 memory 注入路径自动获取，零改动 agent_runner.py。

### LLM 命令配置

`config.json` 中 `knowledge.command`（默认 `minimax`），可改为 `ccs`、`cco` 等。

## 定时任务（routes_ws.py）

所有定时任务均用 asyncio 原生实现，无外部框架依赖。

| 任务 | 实现 | 触发时机 |
|------|------|----------|
| WebSocket 心跳 | `_heartbeat_loop()`，每 20 秒 broadcast ping | 首个 WS 客户端连接时 |
| 每日知识总结 | `_daily_summary_loop()`，每天凌晨 0 点 | 应用启动时（lifespan） |

**每日知识总结流程**：
1. 应用启动 → `lifespan` → `ensure_daily_summary_task()`
2. 循环计算到下一个本地时间凌晨 0 点的秒数，`asyncio.sleep()`
3. 醒来 → 对所有 agent 执行 `_run_daily_summary(agent_id, yesterday)`
4. 按 `task.updated_at` 过滤前一天的任务，无任务则跳过
5. 调用 `generate_summary()`，结果写入 knowledge 文档并更新 memory 索引

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
| GET | `/api/agents/{id}/knowledge` | 读取知识文档（errors/project/hotfiles） |
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
ls data/knowledge/              # 各 Agent 的知识文档
```
