# Fork Task 功能设计

## 概述

从一个已有 Task 的某个阶段，fork 出一个新的 Task。新 Task 继承原 Task 的全部消息历史，用户在新 Task 中发送第一条消息时，通过 `cco --resume <session_id> --fork-session` 启动独立的 claude code 会话分支。

## 核心原理

Claude Code CLI 支持 `--fork-session` 参数：
```bash
cco --resume <session_id> --fork-session <prompt>
```
这会复制 session 的完整对话历史，创建一个新的独立 session（返回新的 session_id），两个 session 互不影响。

## 设计决策：Fork 时不需要 prompt

Fork 操作分两步：
1. **Fork（立即）**：创建新 Task，复制消息历史，记录待 fork 的源 session_id → 新 Task 状态为 `idle`
2. **首次发消息（延迟）**：用户在新 Task 输入框发消息时，使用 `--resume <源sid> --fork-session` 启动 cco → cco 返回新 session_id
3. **后续消息**：走正常的 `--resume <新sid>` 流程

这比 fork 时强制输入 prompt 更自然——用户 fork 出来后在聊天框里正常对话即可。

## 实现方案

### 1. 数据模型：Task 新增 fork_session_id 字段

**文件：`server/models.py`**

```python
class Task(BaseModel):
    ...
    fork_session_id: str | None = None  # 待 fork 的源 session_id（一次性使用）
```

这个字段仅在 fork 创建后、首次发消息前有值。首次发消息启动 cco 后，cco 返回新 session_id，`fork_session_id` 清空。

### 2. 后端：State 新增 fork_task 方法

**文件：`server/state.py`**

新增 `fork_task(source_task_id) -> Task`：
1. 获取源 Task 和其 session_id（无 session 则报错）
2. 创建新 Task（同 agent_id，名称 `"{source_name} (fork)"`，状态 `idle`）
3. 深拷贝源 Task 的 messages（每条消息生成新 id）
4. 设置 `new_task.fork_session_id = source_session_id`
5. 持久化并返回

### 3. AgentRunner：_run_subprocess 支持 fork 模式

**文件：`server/agent_runner.py`**

修改 `_run_subprocess` 中构建命令的逻辑：

```python
# 在构建 args 时
fork_sid = task.fork_session_id
if fork_sid:
    # Fork 模式：--resume <源sid> --fork-session
    args = [command, "-p", "--output-format", "stream-json", ...,
            "--resume", fork_sid, "--fork-session", prompt]
    # 清除 fork 标记（一次性）
    task.fork_session_id = None
elif session_id:
    # 正常 resume 模式
    args = [command, "-p", ..., "--resume", session_id, prompt]
else:
    # 全新会话
    args = [command, "-p", ..., prompt]
```

关键点：
- fork 模式不注入 memory（fork 会话已有完整上下文）
- cco init chunk 返回新 session_id，由已有的 `_handle_chunk` 自动保存到 `_session_ids[task_id]`
- 源 Task 的 session_id 不受影响

### 4. 后端：WebSocket 新增 fork_task 消息类型

**文件：`server/routes_ws.py`**

处理 `fork_task` 消息：
```json
{ "type": "fork_task", "task_id": "source_task_id" }
```

流程：
1. 调用 `app_state.fork_task(source_task_id)` 创建新 task
2. broadcast `task_created`（前端自动切换到新 task）
3. 不启动 cco（等用户发消息）

### 5. 前端：TaskItem 添加 Fork 按钮

**文件：`frontend/src/components/TaskItem.vue`**

在 hover 时显示 Fork 按钮（与删除按钮并列），图标用 ⑂ 或类似分叉符号。点击后直接发 WS 消息，无需弹窗。

### 6. 前端：WebSocket 新增 forkTask 方法

**文件：`frontend/src/composables/useWebSocket.js`**

```js
function forkTask(taskId) {
  send({ type: 'fork_task', task_id: taskId })
}
```

### 7. 前端：agentStore 暴露 session 信息

**文件：`frontend/src/stores/agentStore.js`**

需要让 TaskItem 知道某个 task 是否有 session（用于决定 Fork 按钮是否可用）。通过 `session_update` 消息已有 `updateTaskSession`，确认 task 对象上有 `session_id` 字段可读取即可。

## 数据流

```
用户点击 Fork 按钮
  → WS: { type: "fork_task", task_id: "xxx" }
  → 后端: fork_task() 创建新 Task（复制消息，设置 fork_session_id）
  → broadcast: task_created → 前端显示新 task，自动切换

用户在新 Task 输入消息 "请用方案B重构"
  → WS: { type: "user_message", task_id: "new_xxx", content: "..." }
  → runner.run_task() → 检测到 task.fork_session_id
  → 启动 cco --resume <源sid> --fork-session "请用方案B重构"
  → cco 返回新 session_id → 自动保存
  → 新 Task 独立运行

后续消息
  → 正常 --resume <新sid> 流程，与普通 task 完全一致
```

## 修改文件清单

| 文件 | 改动 |
|---|---|
| `server/models.py` | Task 新增 `fork_session_id` 字段 |
| `server/state.py` | 新增 `fork_task()` 方法 |
| `server/agent_runner.py` | `_run_subprocess()` 检测 `fork_session_id` 构建 fork 命令 |
| `server/routes_ws.py` | 处理 `fork_task` 消息 |
| `frontend/src/components/TaskItem.vue` | 新增 Fork 按钮 |
| `frontend/src/composables/useWebSocket.js` | 新增 `forkTask()` 方法，导出 |

## 边界情况

1. **源 Task 无 session_id**：Fork 按钮隐藏
2. **源 Task 正在 running**：允许 fork（只读取 session_id，不影响源进程）
3. **session 已过期**：用户在新 task 发消息时 cco 会报错，走已有的错误处理流程
4. **Token/Cost 统计**：新 Task 从 0 开始，不继承源 Task 的统计数据
5. **fork_session_id 持久化**：写入 tasks JSON，服务重启后仍可正常 fork
