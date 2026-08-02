# 飞书回复驱动 Task 续话 — 设计

> 目标：把 `/data1/zman/feishu` 的 bot 从"独立 expert 助手"改造为 **agent-park task 的专用飞书前端**。用户在群里回复 task 完成卡片，回复即作为新输入送回对应 task，agent 继续处理，结果回推同一话题。

## 1. 决策：砍掉 expert，bot 只服务 agent-park

用户已确认。这个决策把 §2 的核心复杂度直接消掉了：

- **不再需要"未命中则落回 expert"的兜底分支** —— 没有 expert 了，未命中就是未命中。
- **不再有共用群的行为冲突** —— 群里所有消息只有一种语义：喂给 task。
- **bot 不再需要 `claude-opus` 子进程、`config.yaml` 的 experts 注册表、`ExpertSessionManager`** —— session/续话全部由 agent-park 的 `runner.send_input()`（`--resume`）负责，那本来就是它的职责。

bot 退化成一个**薄的双向传输层**：飞书事件 → HTTP → agent-park；agent-park → `cli.py` → 飞书。

### 为什么这是对的

现在 bot 和 agent-park 是两套**功能重叠**的 agent 调度器：各自起 claude 子进程、各自管 session、各自记对话日志（`logs/conversations/`，131 个会话 17MB）。而 agent-park 的那套明显更完整（持久化、前端可视、token/成本统计、compact、fork、memory/wiki 注入）。保留 expert 等于长期维护两套并行实现。

**已验证的重叠**：expert 的 project_path 有 3/5 已经在 agent-park 里有对应 agent —— `/data1/neo`→`compiler`、`/data1/common/memory`→`memforge`、`/data1/zman/feishu`→`feishu-bot`。缺 `/data1/common/perf` 和 `/data1/common/model-schedule-analysis`（见 §7 待确认）。

## 2. 现状要点

### agent-park

- `server/task_notify.py` → `wiki_notify.send_feishu_card()` → `python3 cli.py send --card --quiet`。
- **`--quiet` 丢掉了 message_id**，所以现在无法关联。这是必须先改的一环。
- 入站只有 WS `user_message` → `runner.send_input(task_id, content)`（`routes_ws.py:322`）。REST 无"向 task 发消息"接口。
- REST 无鉴权，CORS `*`，绑 `0.0.0.0:8001`。agent-park 跑在 **docker 内**（`/.dockerenv` 存在），uvicorn pid 1114389 活着。

### feishu bot

- `bot.py` 用 `lark.ws.Client` **长连接**订阅 `im.message.receive_v1` —— **不需要公网 webhook**，这是本方案可行的关键前提。
- 入站事件带 `message_id` / `root_id` / `parent_id` / `thread_id`（已查 SDK `EventMessage` 确认），但 `bot.py` **完全没用 root_id/parent_id**。
- `send_card()` 支持 `reply_to`（走 `im.message.reply`），能把卡片发进指定话题。
- **bot 进程当前已挂**：`bot.pid`(2650006) 是陈旧的，`logs/bot.log` 最后一条是 6-10 的 `receive message loop exit`。本功能依赖 bot 常驻，需先修复启动与掉线重连。
- `lark_oapi` 只装在 feishu 的 venv，**agent-park venv 里没有** —— 又一个"入站留在 bot 侧"的理由。

## 3. 关联机制

用户回复某条消息时，事件带 `parent_id`(被回复消息) / `root_id`(话题根)。**agent-park 发卡片时记下 `message_id → task_id`，入站回复即可按 `parent_id`/`root_id` 反查 task。** 用户无需记任何 id。

映射的**唯一写者是 agent-park**；bot 不读文件、不缓存、不解析 —— 单一真相源。

> ✅ **已实测验证（2026-08-01）**。扫描该群最近 300 条消息，找到 20 条带 threading 的记录，形态明确：
> ```
> om_…1b52e0…  type=text         parent=None      root=None       ← 用户原始消息
> om_…1b7c30…  type=interactive  parent=om_…1b52e0…  root=om_…1b52e0…  ← bot 回复卡片
> om_…4126a0…  type=interactive  parent=om_…1b52e0…  root=om_…1b52e0…  ← 同话题第二张卡
> ```
> `parent_id` / `root_id` 都被飞书正确填充且指向话题根 —— 同话题多条消息共享同一 `root_id`，正好支撑"一个 task 多张卡片都能反查"与"`--reply-to root_id` 回推同话题"。**方案 A 成立**，无需退到 `thread_id` 或显式 `/task <id>`。

## 4. 数据流

```
① task 结束
   agent-park: task_notify → cli.py send --card（去掉 --quiet，可带 --reply-to）
             → 捕获 stdout 的 message_id（长消息切多张卡 → 多个 id）
             → data/feishu_threads.json: message_id → task_id, task_id → root_id

② 用户在群里回复卡片
   飞书 → bot.py on_message_receive
        → POST http://<agent-park>:8001/api/feishu/inbound
             {message_id, root_id, parent_id, chat_id, sender_open_id, text}
        → bot 按响应回一句 ack/错误提示，不做任何业务判断

③ agent-park
   parent_id → root_id → message_id 顺序反查 task_id
        → 校验 chat_id / sender 白名单 / task 状态
        → 追加 role=user 消息 + broadcast（前端同步可见）
        → runner.send_input(task_id, text)   # --resume 续话

④ task 再次结束 → 回到 ①，带 --reply-to root_id，多轮收拢在同一话题
```

### 未命中怎么办（expert 砍掉后的新问题）

原方案靠"落回 expert"兜底。现在没有兜底了。**已定：明确提示，不静默忽略。** 未命中时回一句"请回复某个 task 卡片以继续该任务"。

不做 `/tasks` / `/new` 命令 —— **已定：不能从飞书创建新 task**。飞书只是"回复已有 task"的入口，新建 task 仍走 Web UI。这让 bot 的职责边界非常干净：它只认识"回复"，不认识"创建"。

## 5. 改动清单

### agent-park

| 文件 | 改动 |
|---|---|
| `server/wiki_notify.py` | `send_feishu_card()` 加 `capture_ids=False` / `reply_to=""`。`capture_ids` 时不传 `--quiet`，解析 stdout 每行一个 id 返回。**默认参数不变 → wiki digest 路径零影响** |
| `server/feishu_threads.py`（新） | 映射读写：`record()` / `resolve()` / 剪枝。原子写（临时文件 + `rename`，同 `sessions.json`） |
| `server/task_notify.py` | 发送后记录映射；发送前查 `by_task` 取 `root_id` 传 `--reply-to` |
| `server/routes_rest.py` | 新增 `POST /api/feishu/inbound` |
| `server/config.py` | `task_notify.inbound` 配置段 |

端点契约：

```jsonc
// 请求
{"message_id":"om_a","root_id":"om_r","parent_id":"om_r",
 "chat_id":"oc_x","sender_open_id":"ou_y","text":"继续处理"}

// 命中并已续话
200 {"matched": true, "task_id": "abc", "action": "resumed",
     "task_name": "修复 X"}                    // bot 用于 ack 文案
// 未命中（非回复 / 话题已剪枝）
200 {"matched": false, "hint": "请回复 task 卡片以继续该任务"}
// 命中但拒绝
200 {"matched": true, "action": "rejected", "reason": "task_running",
     "hint": "agent 正在处理中，请等当前回合结束后再回复"}
// 应用自身消息（含 bot 自己发的 hint / 卡片）—— 最先判断，不查映射
200 {"matched": false, "action": "ignored", "reason": "app_message"}
```

**无 `hint` 字段时 bot 必须闭嘴。** 这不是可选优化：bot 回的 hint 本身是 app 消息，会被再次转发进来，若也回 hint 就成了自己答自己的死循环。因此 `app_message` 分支排在反查之前，且响应里不带 `hint`。

`data/feishu_threads.json`：

```jsonc
{
  "by_message": {"om_a": {"task_id":"abc","root_id":"om_a","chat_id":"oc_x","at":"…"}},
  "by_task":    {"abc":  {"root_id":"om_a","chat_id":"oc_x","at":"…"}}
}
```

一个 task 可能有多个 message_id（`--max-len 3500` 切卡），全部入 `by_message`。剪枝保留最近 500 条或 TTL 30 天。

### feishu bot（大幅**减**码）

**删除**：`call_expert_direct()`、`_handle_expert_direct_async()`、`ExpertSessionManager`、`_find_expert()`、`build_expert_list_card()`、`on_card_action()` 的 expert 分支、`/experts` `/exit` 命令、`config.yaml` 的 `agent` + `experts` 段、`_save_conversation()`（对话历史改由 agent-park 持有）。

**保留**：`lark.ws.Client` 事件订阅、`_get_api_client()`、`send_card()` / `reply_text()` / `split_message()` / `build_reply_card()`、去重 `_processed_message_ids`、30s 旧消息丢弃、`cli.py` 全部（agent-park 出站依赖它）。

**新增**：`on_message_receive` 改为纯转发（无命令）；`agent_park.base_url` 配置；掉线重连（当前已知会 loop exit 后死掉）。

`bot.py` 预计从 ~830 行降到 ~250 行。

### 迁移

- **所有 expert 弃用**（已定）。`config.yaml` 的 `agent` + `experts` 段整段删除，包括 agent-park 里没有对应 agent 的 `/data1/common/perf` 和 `/data1/common/model-schedule-analysis` —— 不补建 agent，直接弃用。
- expert 的 `logs/conversations/`（131 会话 17MB）**保留不删**，只停止写入 —— 历史可查，删除不可逆。

## 6. 分阶段实施

前置事实已全部实测确认（§7），原"阶段 1 验证"已完成，实现从出站 id 捕获开始。

1. **出站 id 捕获**：`send_feishu_card(capture_ids=True)` + `feishu_threads.py` 映射落盘。
   验证：发一张卡 → 检查 `data/feishu_threads.json` 出现 `message_id → task_id`。
2. **入站端点**：`POST /api/feishu/inbound`。
   验证：`curl` 伪造请求（含真实 message_id）→ 确认 task 收到 user 消息并续话。**不依赖 bot**。
3. **bot 瘦身**：删 expert 全部代码，`on_message_receive` 改纯转发；修断线重连 + SIGTERM。
   验证：起 bot → 真实回复卡片 → task 续话，端到端。
4. **话题回推**：`--reply-to root_id`，多轮收拢同一话题。
   验证：连续两轮回复，卡片都落在同一话题内。

阶段 1/2 只改 agent-park、不碰 bot、不影响现有行为。阶段 3 含不可逆删除，**先在 feishu repo 开分支**。

> **服务重启**：全程不自动重启 agent-park（用户明示）。阶段 1/2 的改动需重启后才在线生效 —— 由用户手动执行 `bash run.sh restart`。bot 侧的 `run.sh start/stop` 不受此限制（它不是 agent-park 服务）。

## 7. 前置事实（已全部实测确认）

| 项 | 结论 |
|---|---|
| **网络可达性** | bot 与 agent-park **同容器** `a802ce3f7a2c`，共享 PID namespace。用 feishu venv 请求 `http://127.0.0.1:8001/api/agents` 返回 200/381 agents。`base_url` = `http://127.0.0.1:8001` |
| **关联假设** | 成立，见 §3 实测记录 |
| **出站 id 捕获** | `cli.py` 去掉 `--quiet` 会打印 message_id（实测发卡返回 `om_x100b69ef9f5b28a4b36ce52eb47fd08`） |
| **长连接** | bot 能连上 `msg-frontier.feishu.cn`（实测启动成功） |
| **lark_oapi 位置** | 只在 feishu venv，agent-park venv 没有 → 入站必须留在 bot 侧 |

### 已定决策

| 问题 | 决定 |
|---|---|
| 从飞书新建 task | **不支持**。飞书只作为已有 task 的回复入口 |
| 未命中的回复 | **明确提示**，不静默忽略 |
| task `running` 时收到回复 | **拒绝并提示**，不排队 |
| sender 白名单 | **不做**（见 §8.2） |
| expert | **全部弃用**，不补建缺失 agent |
| 端点鉴权 token | **不加**（风险已知并接受，见 §8.5） |

### 已知缺陷（实现时必须一并修）

- **bot 断线不重连**：`logs/bot.log` 最后一条是 6-10 的 `receive message loop exit, err: no close frame received`，之后进程死掉。不修则功能随机失效。
- **bot 不响应 SIGTERM**：实测 `run.sh stop` 走到"进程未响应，强制终止"才停下。需要正确的信号处理。

## 8. 安全

expert 砍掉后风险**没有降低，反而更集中** —— 群里每条回复现在都直通 agent 会话，而 agent 有 shell 权限：

1. **`chat_id` 必须匹配**配置值，其他群一律拒绝。这是主要的访问控制手段。
2. **sender 白名单：不做。** 实测该群 `member_total` 只有 1 个人类成员（满志远 `ou_70bdd…`），历史 446 条消息全部来自这一个 chat_id、217 条 sender 记录全部同一个 open_id。白名单在当前拓扑下是**零收益的空配置**，只增加一个必须维护的开关。真正的边界是"谁能进这个群"，属于飞书侧群成员管理，不该在 agent-park 里做第二套。
   > 若将来群里加入他人，此结论失效 —— 届时再加白名单，届时它才有意义。
3. **`enabled` 默认 `false`**，与 `task_notify.feishu_notify` 一致。
4. **忽略 app 自身消息**（`sender_type != "app"`）：防"卡片→事件→再发卡片"自激循环。bot 现在没查发送者类型。
5. **8001 无鉴权且绑 `0.0.0.0`**，新端点把暴露面从"读删 task"放大到"驱动 agent 执行任意指令"。
   > **已决定不加 token，接受此风险**（用户明示）。记录在此以便日后回看时知道这是明示决定而非遗漏。
