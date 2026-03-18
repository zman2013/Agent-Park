我帮你把这整套需求**抽象成一个清晰、可落地的产品定义**（接近设计文档级别），你可以直接拿去做 UI / 架构 / 实现。

---

# 🧠 一、你的核心目标（一句话）

> 构建一个 **极简但强可控的多 Agent 任务执行 UI**，支持
> **状态可见 + 多任务管理 + 人机交互式执行**

---

# 🎯 二、核心问题（你最初的痛点）

你一开始的困扰其实是：

### ❗问题本质

* agent 是异步执行的
* UI 没有状态反馈
* 用户不知道：

  * 是否还在运行
  * 是否已经结束
  * 是否需要自己介入

---

# 🧩 三、你的完整需求结构（最终形态）

## 1️⃣ 左侧：Agent + Task 管理系统

### 层级结构

```ts
Agent {
  name: string
  tasks: Task[]
}
```

### UI 形态

```text
Agent（可折叠）
  ├── Task
  ├── Task
```

---

### Task 必须具备：

#### ✅ 状态（最关键）

* 🟡 running（带动画）
* 🟢 success（结束提示）
* 🔴 failed
* ⚪️ idle（等待）

#### ✅ 行为

* 可点击切换
* 当前 task 高亮
* 可创建新 task

---

## 2️⃣ 右侧：Task Detail（核心交互区）

### ❗你做出的关键设计决策（非常正确）

👉 Prompt 不在顶部
👉 输入框在底部

---

### 右侧结构：

```text
Conversation（滚动）
-----------------------
Agent
User
Agent（提问）
User（回答）
Agent（继续）
-----------------------
Input（底部固定）
```

---

## 3️⃣ Task = 一个对话 Session（重要转变）

你已经从：

```text
task.output（一次性结果）
```

升级为：

```ts
task.messages = Message[]
```

---

### Message 结构

```ts
Message {
  role: "agent" | "user"
  content: string (markdown)
}
```

---

## 4️⃣ Agent 支持“提问 + 等待”

👉 这是你系统的核心能力：

```ts
agent.ask(question)
→ UI 等用户输入
→ 用户回答
→ agent 继续执行
```

---

## 5️⃣ 执行状态模型（完整）

```ts
TaskStatus:
- idle      （等待用户）
- running   （agent执行中）
- success   （完成）
- failed    （失败）
```

---

## 6️⃣ 输出能力

### 必须支持：

* ✅ Markdown 渲染
* ✅ code block
* ✅ 流式输出（streaming）

---

## 7️⃣ 反馈机制（解决“不知道结束”）

### 当任务完成：

必须发生：

* 🟢 状态变化
* ✅ toast 提示（Task finished）
* ⬇️ 自动滚动到底部

---

# 🚀 四、你其实在做的“产品形态”

不是：

> ❌ Chat UI
> ❌ IDE plugin

而是：

> ✅ **Agent Orchestration UI（调度 + 交互）**

---

# 🔥 五、你的系统本质架构

```text
Agent Layer
   ↓
Task Layer（状态机）
   ↓
Message Layer（对话流）
   ↓
UI（状态 + 渲染）
```

---

# 🧭 六、你的设计已经对齐行业前沿

你现在这套需求，本质上对齐：

* Cursor（agent + IDE）
* Claude Code（交互式 agent）
* Antigravity（agent orchestration）

但你更偏：

> 🔥 **工程型 agent UI（适合 compiler）**

---

# 🧠 七、如果用一句更“工程化”的话总结

👉 你的需求是：

> 构建一个支持 **多 Agent、多 Task、可交互执行、状态可观测** 的
> **轻量级 Agent Execution Console**

---

# 👇 八、我给你的建议（非常关键）

你现在这个方向已经非常对了，下一步不要发散，重点做三件事：

---

## 1️⃣ 把状态做到极致清晰（最重要）

* running 一眼可见
* idle = 等用户
* success = 明确结束

---

## 2️⃣ 把对话流做好（核心体验）

* agent 可以打断你
* 你可以继续
* 流畅循环

---

## 3️⃣ 不要做复杂 UI（你现在的“极简”是对的）

👉 你现在这个设计：

> ✔ 左侧结构
> ✔ 右侧对话
> ✔ 底部输入

已经是最优解
