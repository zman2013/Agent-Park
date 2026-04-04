# Agent Park

一个用于管理和调度多个 AI Agent 的 Web 平台。**通过本地 LLM CLI 客户端（Claude Code、Codex CLI 等）驱动 Agent 执行**，不直接调用 LLM API，而是复用你已有的本地工具及其认证配置。

通过浏览器界面向 Agent 分配任务、实时查看执行过程，并积累 Agent 的记忆与知识。

## 工作原理

Agent Park 本身不直接与 LLM API 通信。它在后台启动本地 CLI 工具的子进程（如 `claude`、`codex`），通过 PTY 捕获其流式输出，再经 WebSocket 推送到浏览器。

```
浏览器  →  WebSocket  →  Agent Park 后端  →  本地 CLI（claude / codex / ...）  →  LLM
```

这意味着：
- 认证和模型配置完全由本地 CLI 工具管理，无需在 Agent Park 中配置 API Key
- 支持任何以 stream-json 格式输出的本地 LLM 客户端
- 可以同时运行多个不同 CLI 工具的 Agent

## 核心功能

- **多 Agent 管理**：创建和管理多个独立 Agent，每个 Agent 有独立的工作目录、命令配置和任务历史
- **任务调度**：向 Agent 发送任务，支持新会话、续话（resume）和 Fork 三种模式
- **实时流式输出**：通过 WebSocket 实时展示 Agent 的执行过程，包括工具调用和中间输出
- **记忆系统**：每个 Agent 有独立的记忆（JSONL），跨任务积累经验
- **知识总结**：从任务历史中提取错误经验、项目知识和文件热度，自动汇总为结构化文档
- **共享记忆**：多个 Agent 可共享同一份记忆，适用于协同工作场景
- **文件浏览器**：内置文件浏览器，可查看 Agent 工作目录中的文件内容
- **终端面板**：查看 Agent 执行的原始终端输出

## 快速开始

参见 [docs/quickstart.md](docs/quickstart.md)

## 技术栈

- **后端**：Python FastAPI + WebSocket
- **前端**：Vue 3 + Pinia + Vite + Tailwind CSS
- **Agent 执行**：`cco` / `ccs` 命令行工具（Claude Code / Gemini Code）

## 文档

- [快速开始](docs/quickstart.md)
- [代码库说明](docs/codebase.md)

## License

[MIT](LICENSE)
