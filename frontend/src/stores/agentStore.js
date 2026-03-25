import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAgentStore = defineStore('agent', () => {
  const agents = ref([])
  const tasks = ref({})
  const currentTaskId = ref(null)
  const collapsed = ref({})
  const toasts = ref([])
  const pendingAgentOrder = ref(null)
  const pendingAgentOrderRequestId = ref(0)
  let nextAgentOrderRequestId = 0

  // Unseen tasks: tasks that had a status change while not being viewed
  const unseenTaskIds = ref([])

  // Memory panel state
  const memoryPanelOpen = ref(false)
  const memoryAgentId = ref(null)
  const agentMemory = ref({})  // { agent_id: [entries...] }

  const currentTask = computed(() => {
    if (!currentTaskId.value) return null
    return tasks.value[currentTaskId.value] || null
  })

  function cloneMessage(message) {
    return { ...message }
  }

  function mergeMessage(target, source) {
    Object.assign(target, source)
    return target
  }

  function cloneTask(task) {
    return {
      ...task,
      messages: (task.messages || []).map(cloneMessage),
    }
  }

  function mergeTask(target, source) {
    target.agent_id = source.agent_id
    target.name = source.name
    target.prompt = source.prompt
    target.status = source.status
    target.num_turns = source.num_turns
    target.updated_at = source.updated_at

    const existingMessages = new Map((target.messages || []).map(message => [message.id, message]))
    target.messages = (source.messages || []).map((message) => {
      const existing = existingMessages.get(message.id)
      return existing ? mergeMessage(existing, message) : cloneMessage(message)
    })

    return target
  }

  function orderAgents(list, preferredOrder) {
    if (!preferredOrder?.length) return list

    const indexById = new Map(preferredOrder.map((id, index) => [id, index]))
    const ordered = []
    const unordered = []

    for (const agent of list) {
      if (indexById.has(agent.id)) {
        ordered.push(agent)
      } else {
        unordered.push(agent)
      }
    }

    ordered.sort((a, b) => indexById.get(a.id) - indexById.get(b.id))
    return [...ordered, ...unordered]
  }

  function applyAgentOrder(order) {
    agents.value = orderAgents([...agents.value], order)
  }

  function hasSameOrder(left, right) {
    if (!left || !right || left.length !== right.length) return false
    return left.every((id, index) => id === right[index])
  }

  function syncAgents(nextAgents) {
    const existingAgents = new Map(agents.value.map(agent => [agent.id, agent]))
    const mergedAgents = nextAgents.map((agent) => {
      const existing = existingAgents.get(agent.id)
      if (existing) {
        Object.assign(existing, agent)
        return existing
      }
      return { ...agent }
    })
    agents.value = orderAgents(mergedAgents, pendingAgentOrder.value)
  }

  function syncState(data) {
    syncAgents(data.agents || [])
    const newTasks = {}
    for (const [id, task] of Object.entries(data.tasks || {})) {
      const existing = tasks.value[id]
      newTasks[id] = existing ? mergeTask(existing, task) : cloneTask(task)
    }
    tasks.value = newTasks
    if (currentTaskId.value && !tasks.value[currentTaskId.value]) {
      currentTaskId.value = null
    }
    // Add any running tasks to unseenTaskIds (regardless of current selection)
    for (const [id, task] of Object.entries(tasks.value)) {
      if (task.status === 'running') {
        if (!unseenTaskIds.value.includes(id)) {
          unseenTaskIds.value.unshift(id)
        }
      }
    }
  }

  function upsertTask(task) {
    const existing = tasks.value[task.id]
    tasks.value[task.id] = existing ? mergeTask(existing, task) : cloneTask(task)
  }

  function replaceAgentTaskIds(agentId, taskIds) {
    const agent = agents.value.find(item => item.id === agentId)
    if (!agent) return
    agent.task_ids = [...taskIds]
  }

  function updateTaskStatus(taskId, status) {
    const task = tasks.value[taskId]
    if (task) {
      task.status = status
      task.updated_at = new Date().toISOString()
      // Mark as unseen if status is noteworthy
      if (['running', 'success', 'failed', 'waiting'].includes(status)) {
        // For running: always add (even if currently viewed, so it stays visible when user switches away)
        // For others: only add if not currently viewed
        if (status === 'running' || taskId !== currentTaskId.value) {
          if (!unseenTaskIds.value.includes(taskId)) {
            unseenTaskIds.value.unshift(taskId)
          }
        }
      }
    }
  }

  function addMessage(taskId, message) {
    const task = tasks.value[taskId]
    if (!task) return
    // Check if message already exists (by id)
    const existing = task.messages.find(m => m.id === message.id)
    if (!existing) {
      task.messages.push(message)
    }
  }

  function appendChunk(taskId, messageId, delta) {
    const task = tasks.value[taskId]
    if (!task) return
    const msg = task.messages.find(m => m.id === messageId)
    if (msg) {
      msg.content += delta
    }
  }

  function appendChunks(chunks) {
    for (const chunk of chunks) {
      appendChunk(chunk.taskId, chunk.messageId, chunk.delta)
    }
  }

  function markMessageDone(taskId, messageId) {
    const task = tasks.value[taskId]
    if (!task) return
    const msg = task.messages.find(m => m.id === messageId)
    if (msg) {
      msg.streaming = false
    }
  }

  function selectTask(taskId) {
    currentTaskId.value = taskId
    // Only dismiss if the task is not in 'running' state
    const task = tasks.value[taskId]
    if (!task || task.status !== 'running') {
      dismissUnseenTask(taskId)
    }
  }

  function dismissUnseenTask(taskId) {
    const idx = unseenTaskIds.value.indexOf(taskId)
    if (idx !== -1) unseenTaskIds.value.splice(idx, 1)
  }

  function toggleAgent(agentId) {
    collapsed.value[agentId] = !collapsed.value[agentId]
  }

  function isCollapsed(agentId) {
    return !!collapsed.value[agentId]
  }

  let toastId = 0
  function addToast(text, type = 'info') {
    const id = ++toastId
    toasts.value.push({ id, text, type })
    setTimeout(() => {
      removeToast(id)
    }, 4000)
  }

  function removeToast(id) {
    const idx = toasts.value.findIndex(t => t.id === id)
    if (idx !== -1) {
      toasts.value.splice(idx, 1)
    }
  }

  function updateTaskTurns(taskId, numTurns) {
    const task = tasks.value[taskId]
    if (task) {
      task.num_turns = numTurns
    }
  }

  function removeTask(taskId) {
    delete tasks.value[taskId]
    // Remove from agent task_ids
    for (const agent of agents.value) {
      const idx = agent.task_ids.indexOf(taskId)
      if (idx !== -1) agent.task_ids.splice(idx, 1)
    }
    if (currentTaskId.value === taskId) {
      currentTaskId.value = null
    }
    dismissUnseenTask(taskId)
  }

  function updateTaskFields(taskId, fields) {
    const task = tasks.value[taskId]
    if (task) {
      Object.assign(task, fields)
    }
  }

  function addAgent(agent) {
    const existing = agents.value.find(a => a.id === agent.id)
    if (!existing) {
      agents.value.push({ ...agent })
    }
  }

  function updateAgent(agentId, fields) {
    const agent = agents.value.find(a => a.id === agentId)
    if (agent) {
      Object.assign(agent, fields)
    }
  }

  function moveAgentUp(agentId) {
    const idx = agents.value.findIndex(a => a.id === agentId)
    if (idx > 0) {
      const order = agents.value.map(a => a.id)
      const tmp = order[idx - 1]
      order[idx - 1] = order[idx]
      order[idx] = tmp
      applyAgentOrder(order)
      _saveAgentOrder(order)
    }
  }

  function moveAgentDown(agentId) {
    const idx = agents.value.findIndex(a => a.id === agentId)
    if (idx !== -1 && idx < agents.value.length - 1) {
      const order = agents.value.map(a => a.id)
      const tmp = order[idx + 1]
      order[idx + 1] = order[idx]
      order[idx] = tmp
      applyAgentOrder(order)
      _saveAgentOrder(order)
    }
  }

  function handleAgentsReordered(order, requestId = null) {
    const pendingOrder = pendingAgentOrder.value
    const pendingRequestId = pendingAgentOrderRequestId.value

    if (pendingOrder && requestId !== null && requestId < pendingRequestId) {
      return
    }

    applyAgentOrder(order)

    if (!pendingOrder) return

    if (requestId !== null) {
      if (requestId >= pendingRequestId) {
        pendingAgentOrder.value = null
        pendingAgentOrderRequestId.value = 0
      }
      return
    }

    if (hasSameOrder(order, pendingOrder)) {
      pendingAgentOrder.value = null
      pendingAgentOrderRequestId.value = 0
    }
  }

  async function _saveAgentOrder(order) {
    const requestId = ++nextAgentOrderRequestId
    pendingAgentOrder.value = [...order]
    pendingAgentOrderRequestId.value = requestId

    try {
      const res = await fetch('/api/agents/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order, request_id: requestId }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json().catch(() => ({}))
      if (Array.isArray(data.order)) {
        handleAgentsReordered(data.order, data.request_id ?? requestId)
      }
    } catch (e) {
      if (pendingAgentOrderRequestId.value === requestId) {
        pendingAgentOrder.value = null
        pendingAgentOrderRequestId.value = 0
      }
      addToast(`Failed to reorder agents: ${e.message}`, 'error')
    }
  }

  async function pinAgent(agentId) {
    try {
      const res = await fetch(`/api/agents/${agentId}/pin`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
    } catch (e) {
      addToast(`Failed to pin agent: ${e.message}`, 'error')
    }
  }

  async function unpinAgent(agentId) {
    try {
      const res = await fetch(`/api/agents/${agentId}/unpin`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
    } catch (e) {
      addToast(`Failed to unpin agent: ${e.message}`, 'error')
    }
  }

  function openMemoryPanel(agentId) {
    memoryAgentId.value = agentId
    memoryPanelOpen.value = true
  }

  function closeMemoryPanel() {
    memoryPanelOpen.value = false
  }

  function setAgentMemory(agentId, entries) {
    agentMemory.value[agentId] = entries
  }

  function prependMemoryEntry(agentId, entry) {
    if (!agentMemory.value[agentId]) {
      agentMemory.value[agentId] = []
    }
    agentMemory.value[agentId].unshift(entry)
  }

  function removeMemoryEntry(agentId, lineIndex) {
    const entries = agentMemory.value[agentId]
    if (!entries) return
    const idx = entries.findIndex(e => e.line_index === lineIndex)
    if (idx !== -1) entries.splice(idx, 1)
  }

  return {
    agents,
    tasks,
    currentTaskId,
    currentTask,
    collapsed,
    toasts,
    unseenTaskIds,
    memoryPanelOpen,
    memoryAgentId,
    agentMemory,
    syncState,
    updateTaskStatus,
    addMessage,
    appendChunk,
    appendChunks,
    markMessageDone,
    selectTask,
    toggleAgent,
    isCollapsed,
    addToast,
    removeToast,
    removeTask,
    updateAgent,
    updateTaskTurns,
    dismissUnseenTask,
    moveAgentUp,
    moveAgentDown,
    handleAgentsReordered,
    pinAgent,
    unpinAgent,
    openMemoryPanel,
    closeMemoryPanel,
    setAgentMemory,
    prependMemoryEntry,
    removeMemoryEntry,
    upsertTask,
    replaceAgentTaskIds,
    updateTaskFields,
    addAgent,
  }
})
