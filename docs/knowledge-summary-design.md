# Knowledge Summary 设计文档

## 目标

Agent 从每日交互中提炼可复用的知识，沉淀为持久化文档，让后续任务：

1. **不重复犯错** — 错误经验以 checklist 形式阻断
2. **减少探索、节省 token** — 项目知识直接注入，跳过从零摸索
3. **快速定位热点文件** — 无需每次重新搜索

## 存储结构

### 目录布局

```
data/knowledge/{effective_id}/
├── errors.md       # 错误经验
├── project.md      # 项目知识
└── hotfiles.md     # 文件热度统计
```

`effective_id` 复用 `memory.py` 的 `effective_memory_agent_id()` 逻辑：
- agent 设置了 `shared_memory_agent_id` → 使用该 id
- 未设置 → 使用自身 id

**效果：同一项目的多个 worktree agent 共享同一份知识。**

### 三类文档内容

#### errors.md — 错误经验

存储「错误 → 正确做法」的 pair。来源：用户纠正、tool 执行失败、高 turns 任务中的弯路。

```markdown
## 错误经验

### 1. MLIR func 拆分时遗漏 loc 别名
- **错误**: 直接修改 func 签名，未新增 `#loc` 别名
- **正确做法**: 先参考已有二级 func 文件（如 top_Mul_14.mlir）的完整结构
- **出现次数**: 2
- **最近发生**: 2026-04-03

### 2. 测试命令换行导致执行失败
- **错误**: 多行书写 pytest 命令
- **正确做法**: 测试命令写成一行
- **出现次数**: 1
- **最近发生**: 2026-04-01
```

#### project.md — 项目知识

存储项目相关的事实。来源：agent 在 text 消息中对项目的理解总结、用户主动告知的信息。

```markdown
## 项目知识

### 目录结构
- vit_microbenchmark/ 下每个子目录是一个 workload，包含 .mlir 和 schedule.json
- 二级 func 参考模板: top_Mul_14/top_Mul_14.mlir

### 常用命令
- 端到端测试: `python testing/tests/run_single_testcase.py --model_file_path=... --opt_level=... --backend=FM`
- MLIR 可视化: `python scc/compiler/.../mlir_to_mermaid.py <input.mlir> -o <output.html>`

### 约定
- Git commit type 只用 feat 或 fix，不用 docs
- 远程分支: tmp/neo
```

#### hotfiles.md — 文件热度

纯统计，不需要 LLM。记录最近 7 天内 agent 读写频率最高的文件。

```markdown
## 热点文件（最近 7 天）

| 文件 | 读取 | 编辑 | 最近访问 |
|------|------|------|----------|
| vit_head/vit_head.mlir | 12 | 5 | 2026-04-04 |
| vit_head/schedule.json | 8 | 3 | 2026-04-04 |
| NeoLowering.yaml | 6 | 0 | 2026-04-03 |
```

### 文档大小限制

| 文档 | 条目上限 | 字符上限 | 淘汰策略 |
|------|----------|----------|----------|
| errors.md | 10 条 | 2000 字符 | 最旧 + 最低频的先淘汰 |
| project.md | 15 条 | 2000 字符 | 最久未使用的先淘汰 |
| hotfiles.md | 20 个文件 | 1000 字符 | 按加权频率排序，截断 |

### Memory 中的索引

每条知识独立一条 memory 条目，包含该知识点的 summary 和对应文档的索引路径。

每次 summary 后，**删除 memory 中所有旧的 `knowledge_summary` 类型条目**，然后逐条写入新的：

```jsonl
{"type": "knowledge_summary", "timestamp": "2026-04-04T16:00:00Z", "content": "[错误经验] MLIR func 拆分时需先参考已有二级 func 模板（如 top_Mul_14.mlir），否则会遗漏 #loc 别名。详见 data/knowledge/{eid}/errors.md"}
{"type": "knowledge_summary", "timestamp": "2026-04-04T16:00:00Z", "content": "[错误经验] 测试命令必须写成一行，换行会导致执行失败。详见 data/knowledge/{eid}/errors.md"}
{"type": "knowledge_summary", "timestamp": "2026-04-04T16:00:00Z", "content": "[项目知识] vit_microbenchmark/ 下每个子目录是一个 workload，包含 .mlir 和 schedule.json。详见 data/knowledge/{eid}/project.md"}
{"type": "knowledge_summary", "timestamp": "2026-04-04T16:00:00Z", "content": "[项目知识] 端到端测试命令: python testing/tests/run_single_testcase.py --model_file_path=... --backend=FM。详见 data/knowledge/{eid}/project.md"}
{"type": "knowledge_summary", "timestamp": "2026-04-04T16:00:00Z", "content": "[热点文件] 近期高频文件: vit_head.mlir(读12/改5), schedule.json(读8/改3), NeoLowering.yaml(读6)。详见 data/knowledge/{eid}/hotfiles.md"}
```

**每条 memory 的构成：**
- `[类别标签]` — 标明来源（错误经验 / 项目知识 / 热点文件）
- **一句话 summary** — 该知识点的核心内容，agent 看到就能直接用
- **文件索引** — `详见 data/knowledge/{eid}/xxx.md`，需要更多细节时 agent 自行 Read

**为什么每条知识独立一条 memory：**
1. agent 在 memory 中看到的每条都是自包含的、可直接行动的知识
2. 与现有 memory 条目（手动添加的 note）自然混排，无需特殊解析
3. 去重时按 type 过滤即可，不影响其他 memory 条目

## 从 Task 中提取什么

### 信号采集

从 task 的 messages 中识别三类信号，分别喂给不同的提取流程：

#### 错误信号（→ errors.md）

| 信号 | 识别方式 |
|------|----------|
| 用户纠正 | `user:text` 内容含否定/纠正语义（需要 LLM 判断） |
| tool 执行失败 | `agent:tool_result` 内容含 error/traceback/failed |
| 高 turns 弯路 | 同一 task 中 num_turns 显著偏高（超过中位数 2 倍） |
| 任务失败 | task.status == "failed" |

#### 项目知识信号（→ project.md）

| 信号 | 识别方式 |
|------|----------|
| agent 的项目理解 | `agent:text` 中对目录结构、文件关系、命令用法的描述 |
| 用户告知的事实 | `user:text` 中用户主动提供的项目信息（路径、命令、约定） |
| 重复出现的模式 | 多个 task 中 agent 重复探索同一组文件/执行同一组命令 |

#### 文件热度信号（→ hotfiles.md）

| 信号 | 识别方式 |
|------|----------|
| 文件读取 | `agent:tool_use` 中 tool_name=Read 的 file_path |
| 文件编辑 | `agent:tool_use` 中 tool_name=Edit/Write 的 file_path |
| 命令涉及的文件 | `agent:tool_use` 中 tool_name=Bash，从 command 中提取文件路径 |

### 不提取什么

- `tool_result` 的原始内容（文件全文、命令输出）— 太长，不可复用
- 具体的代码 diff — 每次不同
- 流程性对话（"好的"、"已完成"）— 无信息量
- token/cost 等元信息 — 对 agent 执行无帮助

### 提取流程

```
输入: 一组 tasks（当天或指定范围内的所有 tasks）

Step 1: 预处理（无需 LLM）
  - 从所有 messages 中按上述信号分类
  - 从 tool_use 中统计文件访问频率 → 直接更新 hotfiles.md
  - 过滤出「错误相关消息」和「知识相关消息」

Step 2: 提取（需要 LLM）
  - 将错误相关消息 + 现有 errors.md → LLM → 输出合并后的新 errors.md
  - 将知识相关消息 + 现有 project.md → LLM → 输出合并后的新 project.md

Step 3: 写入
  - 覆盖写 errors.md, project.md, hotfiles.md
  - 从文档内容中逐条构建 memory 条目（每条知识一条 memory）
  - 删除 memory 中所有旧的 knowledge_summary 条目，写入新条目
```

## 每日合并（去重）

合并不是追加，而是「旧文档 + 新内容 → LLM → 全新文档」。

### errors.md 合并规则

LLM prompt 中的指令：
1. 如果新提取的错误与已有条目描述同一类问题，合并为一条，出现次数 +1
2. 按严重程度 × 出现频率排序
3. 超过 10 条时，淘汰「出现次数最少 + 最近发生时间最早」的条目
4. 输出不超过 2000 字符

### project.md 合并规则

LLM prompt 中的指令：
1. 同一主题的知识（如同一文件/目录的描述），用最新的覆盖旧的
2. 保留分类结构（目录结构、常用命令、约定）
3. 超过 15 条时，淘汰「最久没被 task 间接引用」的条目
4. 输出不超过 2000 字符

### hotfiles.md 合并规则

无需 LLM，纯计算：
1. 最近 7 天的文件访问记录做加权统计（越近权重越高）
2. 按加权频率排序，保留 top 20
3. 直接覆盖写入

## 知识注入

### 核心思路：复用现有 memory 机制

**不新增独立的知识注入逻辑。** summary 完成后，将每条知识的 summary + 文件索引逐条写入 memory（JSONL），agent 启动时通过现有的 memory 注入路径自动获取。

零改动 `agent_runner.py`。

### 注入效果

agent 启动时，memory 中会包含类似这样的条目：

```
[错误经验] MLIR func 拆分时需先参考已有二级 func 模板（如 top_Mul_14.mlir），否则会遗漏 #loc 别名。详见 data/knowledge/{eid}/errors.md
[项目知识] vit_microbenchmark/ 下每个子目录是一个 workload，包含 .mlir 和 schedule.json。详见 data/knowledge/{eid}/project.md
```

agent 直接就能看到关键知识。如果需要更多细节（比如完整的错误经验列表），可以自行 Read 对应的 .md 文件。

### 为什么这样做

1. memory 注入已经是成熟机制，不增加任何新的注入路径
2. 每条知识自包含，agent 看到就能直接行动
3. 需要细节时，agent 可以主动 Read 文件索引指向的 .md 文档

## 触发方式

### 手动触发（首先实现）

前端 UI 入口 → WebSocket 消息 → 后端执行。

**WebSocket 协议扩展：**

客户端 → 服务端：
```json
{
  "type": "generate_summary",
  "agent_id": "xxx",
  "date_range": "today"
}
```

`date_range` 可选值：
- `"today"` — 当天所有 tasks
- `"recent_n"` — 最近 N 个已完成的 tasks（N 通过配置控制，默认 5）

服务端 → 客户端（进度）：
```json
{"type": "summary_progress", "agent_id": "xxx", "step": "extracting", "detail": "分析 3 个任务..."}
{"type": "summary_progress", "agent_id": "xxx", "step": "merging", "detail": "合并 errors.md..."}
{"type": "summary_done", "agent_id": "xxx", "files_updated": ["errors.md", "project.md", "hotfiles.md"]}
```

### 前端 UI

在 MemoryPanel 或 AgentGroup 面板中添加入口：
- 一个「生成知识总结」按钮
- 点击后显示进度状态
- 完成后可在 MemoryPanel 中新增「知识」标签页，展示三个文档内容

## 后端模块设计

### 新增文件: `server/knowledge.py`

```
knowledge.py
├── effective_knowledge_dir(agent_id) -> Path
├── extract_error_signals(tasks) -> list[dict]       # 从 messages 提取错误信号
├── extract_project_signals(tasks) -> list[dict]     # 从 messages 提取知识信号
├── compute_hotfiles(tasks) -> list[dict]            # 统计文件访问频率
├── merge_errors(existing_md, new_signals, command) -> str    # LLM 合并
├── merge_project(existing_md, new_signals, command) -> str   # LLM 合并
├── write_hotfiles(agent_id, hotfiles) -> None       # 直接写入
├── build_memory_entries(eid, errors_md, project_md, hotfiles) -> list[dict]  # 逐条构建 memory 条目
├── generate_summary(agent_id, tasks) -> dict        # 主入口，协调上述流程
└── update_memory_index(agent_id, entries)           # 删旧 knowledge_summary + 逐条写入新条目
```

### 修改文件

| 文件 | 修改内容 |
|------|----------|
| `server/routes_ws.py` | 新增 `generate_summary` 消息处理 |
| `server/routes_rest.py` | 新增 GET `/agents/{id}/knowledge` 读取文档 |
| `server/config.py` | 新增 `knowledge_config()` 读取配置 |
| `config.json` | 新增 `knowledge` 配置段 |
| `frontend/.../MemoryPanel.vue` | 新增「知识」标签页 |
| `frontend/.../AgentGroup.vue` | 新增「生成知识总结」按钮 |

**注意：不需要修改 `agent_runner.py`**，知识通过 memory 注入。

### 配置扩展 (config.json)

```json
{
  "knowledge": {
    "command": "minimax",
    "errors_max_items": 10,
    "errors_max_chars": 2000,
    "project_max_items": 15,
    "project_max_chars": 2000,
    "hotfiles_max_items": 20,
    "hotfiles_recent_days": 7,
    "default_task_count": 5
  }
}
```

`command` 默认为 `minimax`，用低成本模型做 summary 提取和合并。可在 config.json 中修改为其他命令（如 `ccs`、`cco`）。

## LLM Prompt 设计

### 错误经验提取 + 合并 Prompt

```
你是一个经验分析器。从以下对话记录中提取「错误 → 正确做法」的经验对。

## 输入

### 当前已有的错误经验文档
{existing_errors_md}

### 今日新的对话记录（仅含错误相关片段）
{error_signals_text}

## 要求

1. 从新对话中识别：用户纠正了什么、哪里执行失败了、哪里走了弯路
2. 提取为「错误描述 → 正确做法」的结构
3. 与已有文档合并：相同错误模式合并，出现次数累加
4. 按频率 × 严重程度排序
5. 最多保留 {max_items} 条，不超过 {max_chars} 字符
6. 输出纯 Markdown，无需代码块包裹

## 输出格式

### 1. [错误简述]
- **错误**: ...
- **正确做法**: ...
- **出现次数**: N
- **最近发生**: YYYY-MM-DD
```

### 项目知识提取 + 合并 Prompt

```
你是一个知识整理器。从以下对话记录中提取可复用的项目知识。

## 输入

### 当前已有的项目知识文档
{existing_project_md}

### 今日新的对话记录（仅含知识相关片段）
{project_signals_text}

## 要求

1. 从新对话中识别：项目结构描述、文件用途、常用命令、团队约定、关键路径
2. 与已有文档合并：同一主题用最新信息覆盖旧的
3. 保持分类结构（目录结构、常用命令、约定、关键文件）
4. 最多保留 {max_items} 条知识点，不超过 {max_chars} 字符
5. 只保留对后续任务有直接帮助的信息，去掉一次性的具体细节
6. 输出纯 Markdown，无需代码块包裹

## 输出格式

按分类组织，每条知识点简洁明确，可直接作为 agent 的参考信息。
```

## 实现顺序

1. `server/knowledge.py` — 核心逻辑（提取、合并、写入文档、更新 memory 索引）
2. `server/config.py` + `config.json` — 新增 `knowledge` 配置段（command 默认 `ccm`）
3. `server/routes_ws.py` + `routes_rest.py` — API 接口（触发 summary + 读取文档）
4. `frontend` — UI 入口（生成按钮）+ 知识展示面板
