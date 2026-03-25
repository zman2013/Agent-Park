# Codebase Overview

> 阅读本文档可快速了解项目结构，无需每次重新探索代码库。

## 技术栈

- **后端**：Python FastAPI + WebSocket，`server/` 目录
- **前端**：Vue 3 + Pinia + Vite，`frontend/` 目录
- **数据持久化**：JSON 文件（`data/` 目录）
- **Agent 执行**：`os.fork()` + PTY + `cco` 命令行工具（stream-json 协议）

## 目录结构

```
agent-park/
├── server/              # Python 后端
│   ├── main.py          # FastAPI 入口，挂载路由
│   ├── models.py        # 数据模型（Agent、Task、Message、TaskStatus）
│   ├── state.py         # 内存状态管理 + JSON 持久化（AppState）
│   ├── agent_runner.py  # 核心：子进程管理、PTY、流式输出处理
│   ├── routes_ws.py     # WebSocket 路由，消息分发，broadcast()
│   ├── routes_rest.py   # REST API（Agent/Task CRUD、Memory）
│   ├── memory.py        # Agent 记忆读写（JSONL 格式）
│   └── config.py        # 读取 config.json 配置
├── frontend/
│   └── src/
│       ├── stores/agentStore.js          # Pinia 全局状态
│       ├── composables/useWebSocket.js   # WebSocket 连接与消息处理
│       └── components/
│           ├── ChatView.vue              # 聊天面板，流式消息展示
│           ├── TaskItem.vue              # 任务列表项
│           └── AgentGroup.vue            # Agent 分组面板
├── data/
│   ├── tasks.json       # 所有 Agent 和 Task 的持久化存储
│   ├── sessions.json    # cco 会话 ID（用于续话）
│   └── memory/          # 每个 Agent 的记忆文件（{agent_id}.jsonl）
├── docs/                # 项目文档
├── config.json          # 全局配置（memory、端口等）
├── run.sh               # 启动/停止/重启脚本
├── AGENTS.md            # Agent 操作指引（本文件的入口）
└── requirements.txt
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
    id: str               # 12 字符 UID
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
    updated_at: str       # ISO UTC 时间戳

class Message(BaseModel):
    id: str
    role: str             # "user" | "agent"
    type: str             # "text" | "tool_use" | "tool_result" | "system"
    content: str
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

### 服务端 → 客户端（broadcast）

| type | 说明 |
|---|---|
| `task_created` | 任务创建完成 |
| `task_status` | 状态变更（running/success/failed） |
| `message` | 新消息（含 streaming=true 的流式块） |
| `message_chunk` | 流式增量内容 |
| `message_done` | 消息流结束 |
| `full_state` | 连接建立时下发完整状态 |

## 状态持久化（state.py）

- `AppState` 单例，保存所有 agents 和 tasks
- 写操作后调用 `save_tasks()` → 写入 `data/tasks.json`
- `save_sessions()` → 写入 `data/sessions.json`（cco 续话用）
- 启动时从 JSON 恢复，running/waiting 任务重置为 failed

## 前端状态管理（agentStore.js）

- `agents`：Agent 列表（含 pinned 排序）
- `tasks`：任务 Map（key: task_id）
- `activeTaskId`：当前选中任务
- WebSocket 消息驱动状态更新，无需手动轮询

## 常用操作参考

```bash
# 启动/重启服务
./run.sh start
./run.sh restart

# 查看日志
tail -f logs/backend.log
tail -f logs/frontend.log

# 数据文件
cat data/tasks.json    # Agent + Task 数据
cat data/sessions.json # cco 会话 ID
```
