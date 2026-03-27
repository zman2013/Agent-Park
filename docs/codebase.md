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
├── config.json              # 全局配置（端口、默认 Agent 列表）
├── run.sh                   # 启动/停止/重启脚本
├── requirements.txt
├── pyproject.toml
├── server/                  # Python 后端
│   ├── main.py              # FastAPI 入口，挂载路由
│   ├── models.py            # 数据模型（Agent、Task、Message、TaskStatus）
│   ├── state.py             # 内存状态管理 + JSON 持久化（AppState）(205行)
│   ├── agent_runner.py      # 核心：子进程管理、PTY、流式输出处理 (837行)
│   ├── routes_ws.py         # WebSocket 路由，消息分发，broadcast() (132行)
│   ├── routes_rest.py       # REST API（Agent/Task CRUD、Memory）(404行)
│   ├── memory.py            # Agent 记忆读写（JSONL 格式）(170行)
│   └── config.py            # 读取 config.json 配置
├── frontend/
│   └── src/
│       ├── App.vue                           # 根组件（可拖拽左右布局）
│       ├── main.js                           # Vue 应用入口，挂载 Pinia
│       ├── stores/agentStore.js              # Pinia 全局状态 (445行)
│       ├── composables/useWebSocket.js       # WebSocket 连接与消息处理 (286行)
│       └── components/
│           ├── AgentTree.vue                 # 左侧 Agent 树形列表
│           ├── AgentGroup.vue                # Agent 分组面板 (345行)
│           ├── ChatView.vue                  # 聊天面板，流式消息展示 (224行)
│           ├── ChatInput.vue                 # 用户输入框
│           ├── TaskItem.vue                  # 任务列表项
│           ├── MessageBubble.vue             # 消息气泡（Markdown + 代码高亮）(324行)
│           ├── MemoryPanel.vue               # Agent 记忆面板
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
│   │   └── {agent_id}.json  # 每个 Agent 的所有 Task
│   └── memory/              # Agent 记忆（JSONL 格式）
│       └── {agent_id}.jsonl
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
  2. 构建 cco 命令参数（含 --resume session_id 若有续话）
  3. os.fork() + pty.openpty() 启动子进程
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

## WebSocket 消息协议（routes_ws.py ↔ useWebSocket.js）

### 客户端 → 服务端

| type | 说明 |
|---|---|
| `create_task` | 创建新任务 |
| `user_message` | 发送消息（触发 run_task） |
| `stop_task` | 中止任务 |
| `set_agent_order` | 重排序 Agent |

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

## 状态持久化（state.py）

- `AppState` 单例，内存中维护所有 agents 和 tasks
- `save_agents()` → 写入 `data/agents.json`（Agent 元数据 + 排序）
- `save_agent_tasks(agent_id)` → 写入 `data/tasks/{agent_id}.json`（按 Agent 分离）
- `save_sessions()` → 写入 `data/sessions.json`（cco 续话用）
- 启动时从 JSON 恢复，running/waiting 任务重置为 failed

## Agent 记忆管理（memory.py）

- 格式：JSONL，每行一条 `MemoryEntry`（timestamp、role、content、tokens）
- 文件位置：`data/memory/{agent_id}.jsonl`
- 支持多 Agent 共享记忆（通过 `shared_memory_agent_id`）
- 最大行数由 `config.json` 的 `memory.max_lines` 控制

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
```
