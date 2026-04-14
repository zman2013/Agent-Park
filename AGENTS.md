# Agent Instructions

## 开始前必读

**每次开始任务前，先阅读 [`docs/codebase.md`](docs/codebase.md)**，其中包含：

- 项目架构与目录结构
- 核心数据模型（Agent、Task、Message）
- 任务执行流程（agent_runner.py 详解）
- WebSocket 消息协议
- 错误处理机制
- 前端状态管理

阅读该文档可避免重复探索代码库，直接定位到相关文件。

## 服务管理

使用项目根目录的 `run.sh` 管理服务，支持以下参数：

```bash
bash run.sh start    # 启动所有服务
bash run.sh stop     # 停止所有服务
bash run.sh restart  # 重启所有服务（stop + start）
bash run.sh status   # 查看运行状态
```

需要重启服务时，直接执行：

```bash
bash run.sh restart
```

## 临时文件规则

- **禁止在项目根目录或 docs/ 下创建临时目录/文件**（如 `pages/`、`docs/wiki/`、`wiki-merge/`、`*.json` 中间产物等）
- 所有临时工作目录和中间产物统一输出到 `out/` 目录
- `out/` 已在 `.gitignore` 中，不会被 git 追踪
- wiki ingest 的实际写入目标是 `/data1/common/wiki/{wiki_name}/`，不是 `docs/wiki/`