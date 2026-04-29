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

- **多 Agent 管理**：创建和管理多个独立 Agent，每个 Agent 有独立的工作目录、命令配置、Wiki 关联和任务历史
- **任务调度**：向 Agent 发送任务，支持新会话、续话（resume）和 Fork 三种模式
- **实时流式输出**：通过 WebSocket 实时展示 Agent 的执行过程，包括工具调用和中间输出
- **记忆系统**：每个 Agent 有独立的记忆（JSONL），跨任务积累经验
- **知识总结**：从任务历史中提取错误经验、项目知识和文件热度，自动汇总为结构化文档
- **Wiki 知识沉淀**：为 Agent 配置 Wiki 后，自动从成功任务中提取技术决策、Bug 根因、调试方法论等知识点，增量合并到 Markdown Wiki 中
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

## Wiki Ingest（知识库增量沉淀）

为 Agent 配置 `wiki` 字段后，`scripts/wiki_ingest.py` 脚本会自动从成功完成的任务中提取知识点并合并到 Wiki 中：

```bash
cd /data1/common/agent-park
python scripts/wiki_ingest.py              # 处理所有未 ingest 的 task
python scripts/wiki_ingest.py --date 2026-04-13  # 只处理指定日期的 task
```

### 工作流程

1. **消息提取** — 从 task 的 messages 中提取 text 类型对话内容
2. **知识提取** — 调用 LLM 分析对话，提取技术决策、Bug 根因、调试方法等知识点
3. **Wiki 合并** — 读取目标 Wiki 的 index.md，让 LLM 决定合并到已有页面还是创建新页面
4. **去重记录** — 每个 task 只 ingest 一次，记录在 `ingested.json` 中

### 配置

在 `config.json` 中配置 `wiki_ingest` 和 `feishu_notify`：

```json
{
  "wiki_ingest": {
    "command": "qwen",
    "wiki_base": "/data1/common/wiki",
    "timeout": 300,
    "max_message_chars": 50000,
    "memforge_reindex_enabled": false,
    "memforge_reindex_script": "/data1/common/memory/scripts/memforge.sh",
    "memforge_reindex_timeout": 600,
    "feishu_notify": {
      "enabled": true,
      "cli_path": "/data1/zman/feishu/cli.py",
      "chat_id": "oc_xxx",
      "env_file": "/data1/zman/feishu/.env"
    }
  }
}
```

- `wiki_base`：**必填**，存放所有 wiki 的根目录绝对路径（无默认值）。未配置时 ingest/search 会拒绝运行
- `memforge_reindex_enabled`：ingest 完成后是否触发一次 memforge 向量索引刷新。默认 `false`，保持纯本地 wiki 行为不变；设为 `true` 后，在批量模式和单 task 模式末尾都会调用一次 `memforge_reindex_script`（失败只记日志，不影响 ingest 主流程）。
- `memforge_reindex_script`：memforge 提供的统一入口脚本路径。启用 reindex 时必填，无默认值。
- `memforge_reindex_timeout`：reindex 超时秒数，默认 600。

### Cron 定时执行

在宿主机 crontab 中配置：

```cron
0 2 * * * docker exec agent-park bash -c "cd /data1/common/agent-park && python scripts/wiki_ingest.py" >> /var/log/wiki-ingest.log 2>&1
```

## Wiki 预检索（Agent 启动前知识注入）

Agent 执行任务前，会自动从关联的 Wiki 知识库中检索相关知识并注入到 prompt 中，让 Agent "带着领域知识开始工作"。

### 工作原理

1. Agent 收到 prompt 时，读取关联 Wiki 的 `index.md` 索引
2. 调用 LLM 从索引中选出与当前任务相关的页面（最多 5 个）
3. 读取匹配页面的 frontmatter（title / summary / overview）
4. 组装 `<wiki-context>` 块，插入到 memory 之后、原始 prompt 之前

最终 prompt 结构：

```
<memory>...agent 记忆...</memory>

<wiki-context>
...wiki 知识摘要 + 页面链接...
</wiki-context>

用户的原始 prompt
```

### 跳过检索

在 prompt 前加 `!wiki` 前缀可跳过 wiki 预检索（如 `!wiki 帮我做 X`），前缀会被自动剥离。

### 配置

在 `config.json` 中配置 `wiki_search`（可选，默认复用 `wiki_ingest` 配置）：

```json
{
  "wiki_search": {
    "backend": "local",
    "memforge_script": "/data1/common/memory/scripts/memforge.sh",
    "command": "qwen",
    "timeout": 30,
    "max_pages": 5,
    "top_k": 5
  }
}
```

- `backend`：检索后端，`local`（默认）或 `memforge`
  - `local`：现有行为——读取 `{wiki}/index.md`，调 LLM 从索引中选页
  - `memforge`：走 memforge 向量检索，通过统一入口脚本 `memforge_script` 调用；运行时若脚本缺失或调用失败，自动降级为 `local`，不会让 agent 看到异常
- `memforge_script`：memforge 提供的统一入口脚本路径。`backend=memforge` 时必填，无默认值。
- `command`：LLM 命令（仅 `local` backend 使用），默认复用 `wiki_ingest.command`
- `timeout`：超时秒数，默认 30s（更短的超时，不能让用户等太久）
- `max_pages`：最大返回页面数，默认 5
- `top_k`：memforge 语义检索请求的候选数量，默认 5（内部会按 wiki 名过滤后再截断到 `max_pages`）
- `wiki_base`：默认复用 `wiki_ingest.wiki_base`（同样无硬编码默认值）

### memforge 联动

启用 `memforge` backend 后，整个闭环是：

1. 每日/单次 ingest 把新知识沉淀到 `/data1/common/wiki/{wiki_name}/`；
2. `wiki_ingest.memforge_reindex_enabled = true` 时，ingest 结束调用 `memforge.sh reindex --kind wiki --quiet` 增量索引到 Chroma；
3. 下次 agent 启动 task 时，`wiki_search` 走 `memforge.sh search --query - --kind wiki --format json`，拿到语义相关的 wiki 页面后按现有格式拼成 `<wiki-context>` 注入 prompt。

agent-park 对 memforge 的所有调用只依赖一个统一脚本入口（子命令 `search` / `reindex`），不感知 memforge 的 Python 环境、模块路径或 embedding 模型。

## AgentLoop（design.md 驱动的自动循环）

`agentloop/` 是一个内嵌的独立 CLI 子项目：给一份 `design.md`，自动调度 **planner / PM / dev / qa** 四个角色顺序协作，跑到所有任务完成。

与 agent-park 平台解耦 —— 不依赖服务端，纯 CLI，自己起 `cco` / `ccs` 子进程执行。适合"无人值守"把一个明确定义的小项目从零做到交付。

```bash
cd /data1/common/agent-park
python -m agentloop run ~/myproj/design.md        # 跑或续跑
python -m agentloop status ~/myproj/design.md     # 看进度
python -m agentloop resume ~/myproj/design.md --more-cycles 20  # 预算追加
```

核心特性：

- **文件契约通信**：角色只通过 `todolist.md` 传递信息（context quarantine），不共享对话历史
- **design.md 全程只读**：防止需求飘移；改 design 必须人类介入
- **状态全落盘**：随时中断随时恢复，`.agentloop/state.json` + `todolist.md` 是唯一真相源
- **失败自重试**：dev 被 qa 打回会带 `attempt_log` 再试，`max_item_attempts` 上限兜底
- **成本上限**：从 cco/ccs 的 stream-json 抽 `total_cost_usd` 累加，超 `max_cost_cny` 自动退出
- **每角色独立后端**：qa 配 ccs（便宜）、dev/planner 配 cco（能打）、PM 代码版（不花钱）

完整文档：

- [agentloop/README.md](agentloop/README.md) — 能力总览
- [agentloop/docs/quickstart.md](agentloop/docs/quickstart.md) — 5 分钟上手
- [agentloop/DESIGN.md](agentloop/DESIGN.md) — 状态机、权限矩阵、回滚语义

## License

[MIT](LICENSE)
