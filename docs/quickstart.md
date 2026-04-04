# Agent Park - Quick Start

多 Agent 任务执行 UI，支持状态可见、多任务管理、人机交互式执行。

## 环境要求

- Python 3.10+
- Node.js 18+
- [uv](https://docs.astral.sh/uv/)（Python 包管理）
- `cco`（Claude Code CLI）

## 安装

```bash
# 克隆项目
git clone <repo-url> && cd agent-park

# Python 依赖
uv venv && uv pip install -e .

# 前端依赖
cd frontend && npm install && cd ..
```

## 配置

编辑项目根目录 `config.json`：

```json
{
  "server": {
    "host": "0.0.0.0",
    "port": 8001
  },
  "frontend": {
    "port": 3000
  },
  "agents": [
    { "name": "Scheduler", "command": "cco", "cwd": "" },
    { "name": "Codegen",   "command": "cco", "cwd": "/path/to/project" },
    { "name": "Reviewer",  "command": "cco", "cwd": "" }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `server.host` | 后端监听地址 |
| `server.port` | 后端端口 |
| `frontend.port` | 前端 dev server 端口 |
| `agents[].name` | Agent 显示名称 |
| `agents[].command` | Agent 命令（默认 `cco`） |
| `agents[].cwd` | Agent 工作目录（留空则使用项目根目录） |

## 启动 / 停止

```bash
./run.sh start    # 启动 backend + frontend
./run.sh stop     # 停止所有服务
./run.sh restart  # 重启
./run.sh status   # 查看运行状态
```

日志输出在 `logs/` 目录（`backend.log`、`frontend.log`）。

## 使用

1. 浏览器打开 `http://<host>:<frontend-port>`
2. 左侧面板显示所有 Agent，点击 ⚙ 可编辑名称和工作目录
3. 点击 `+ new task` 输入 prompt 创建任务
4. 右侧面板实时显示 Agent 的流式输出
5. Agent 执行结束后状态变为绿色（成功）或红色（失败）

## 项目结构

```
agent-park/
  config.json          # 统一配置
  run.sh               # 启停脚本
  server/
    main.py            # FastAPI 入口
    config.py          # 配置加载
    models.py          # 数据模型
    state.py           # 内存状态
    routes_rest.py     # REST API
    routes_ws.py       # WebSocket
    agent_runner.py    # cco 子进程管理（PTY）
  frontend/
    src/
      App.vue          # 根布局
      stores/           # Pinia 状态管理
      composables/      # WebSocket 连接
      components/       # Vue 组件
```
