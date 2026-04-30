import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAgentStore = defineStore('agent', () => {
  const agents = ref([])
  const tasks = ref({})
  const taskSessions = ref({})  // { task_id: session_id }
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

  // Prompts panel state
  const promptsPanelOpen = ref(false)

  // Archived agent filter
  const showArchived = ref(false)

  // ── AgentLoop state ────────────────────────────────────────────────────────
  // Currently selected agentloop shown in the center area (mutually exclusive with currentTaskId)
  const selectedAgentLoopId = ref(null)
  // Full list of non-dismissed agentloops (for both sidebar "recent" and task-header link lookup)
  const agentloops = ref([])
  // Detailed snapshot for the currently selected agentloop (state + todolist + runs)
  const agentloopSnapshot = ref(null)
  // Run log for the currently viewed cycle
  const agentloopRunLog = ref({ cycle: null, lines: [] })

  // Recent files (per agent, localStorage-backed)
  const RECENT_FILES_KEY = 'agent-park:recent-files'
  const MAX_RECENT_PER_AGENT = 20
  const recentFiles = ref(JSON.parse(localStorage.getItem(RECENT_FILES_KEY) || '{}'))

  function removeRecentFile(agentId, filePath) {
    const list = recentFiles.value[agentId]
    if (!list) return
    const idx = list.findIndex(f => f.path === filePath)
    if (idx !== -1) list.splice(idx, 1)
    localStorage.setItem(RECENT_FILES_KEY, JSON.stringify(recentFiles.value))
  }

  function addRecentFile(agentId, filePath) {
    if (!recentFiles.value[agentId]) recentFiles.value[agentId] = []
    const list = recentFiles.value[agentId]
    const idx = list.findIndex(f => f.path === filePath)
    if (idx !== -1) list.splice(idx, 1)
    list.unshift({ path: filePath, name: filePath.split('/').pop(), openedAt: Date.now() })
    if (list.length > MAX_RECENT_PER_AGENT) list.length = MAX_RECENT_PER_AGENT
    localStorage.setItem(RECENT_FILES_KEY, JSON.stringify(recentFiles.value))
  }

  function getRecentFiles(agentId) {
    return recentFiles.value[agentId] || []
  }

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
    target.total_input_tokens = source.total_input_tokens ?? target.total_input_tokens ?? 0
    target.total_output_tokens = source.total_output_tokens ?? target.total_output_tokens ?? 0
    target.context_window = source.context_window ?? target.context_window ?? 0
    target.total_cost_cny = source.total_cost_cny ?? target.total_cost_cny ?? 0
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
    // Sync sessions
    if (data.sessions) {
      Object.assign(taskSessions.value, data.sessions)
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
    // Entering a task exits agentloop view
    if (selectedAgentLoopId.value) {
      selectedAgentLoopId.value = null
      agentloopSnapshot.value = null
      agentloopRunLog.value = { cycle: null, lines: [] }
    }
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

  function updateTaskTurns(taskId, numTurns, tokenInfo = {}) {
    const task = tasks.value[taskId]
    if (task) {
      task.num_turns = numTurns
      if (tokenInfo.total_input_tokens !== undefined) task.total_input_tokens = tokenInfo.total_input_tokens
      if (tokenInfo.total_output_tokens !== undefined) task.total_output_tokens = tokenInfo.total_output_tokens
      if (tokenInfo.context_window !== undefined) task.context_window = tokenInfo.context_window
      if (tokenInfo.total_cost_cny !== undefined) task.total_cost_cny = tokenInfo.total_cost_cny
      if (tokenInfo.model_usage !== undefined) task.model_usage = tokenInfo.model_usage
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

  function updateTaskSession(taskId, sessionId) {
    taskSessions.value[taskId] = sessionId
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

  async function archiveAgent(agentId) {
    try {
      const res = await fetch(`/api/agents/${agentId}/archive`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
    } catch (e) {
      addToast(`Failed to archive agent: ${e.message}`, 'error')
    }
  }

  async function unarchiveAgent(agentId) {
    try {
      const res = await fetch(`/api/agents/${agentId}/unarchive`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
    } catch (e) {
      addToast(`Failed to unarchive agent: ${e.message}`, 'error')
    }
  }

  function openMemoryPanel(agentId) {
    memoryAgentId.value = agentId
    memoryPanelOpen.value = true
  }

  function closeMemoryPanel() {
    memoryPanelOpen.value = false
  }

  function openPromptsPanel() {
    promptsPanelOpen.value = true
  }

  function closePromptsPanel() {
    promptsPanelOpen.value = false
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

  // ── AgentLoop actions ──────────────────────────────────────────────────────

  async function fetchAgentLoops() {
    try {
      const res = await fetch('/api/agentloops?include_dismissed=true')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      agentloops.value = Array.isArray(data) ? data : []
    } catch (e) {
      // silent — avoid toast spam on every poll failure
    }
  }

  // Find an agentloop (including dismissed) registered for the given cwd.
  // Returns the most recently started entry so TaskItem / ChatView have a
  // single obvious button target when a cwd has multiple workspaces.
  function findAgentLoopByCwd(cwd) {
    if (!cwd) return null
    const matches = findAgentLoopsByCwd(cwd)
    return matches[0] || null
  }

  // All agentloops (including dismissed) registered for the given cwd, sorted
  // newest-first. Callers that need to offer a switcher (multi-workspace UI)
  // use this; single-entry consumers stay on findAgentLoopByCwd.
  function findAgentLoopsByCwd(cwd) {
    if (!cwd) return []
    return (agentloops.value || [])
      .filter(l => l.cwd === cwd)
      .slice()
      .sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''))
  }

  async function fetchAgentLoopSnapshot(loopId) {
    // Capture which loop was selected at call time so a stale response from a
    // previous loop (user switched before this request resolved) can't
    // overwrite the now-current snapshot.
    const requestedFor = loopId
    try {
      const res = await fetch(`/api/agentloops/${loopId}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      if (selectedAgentLoopId.value !== requestedFor) return
      agentloopSnapshot.value = data
    } catch (e) {
      // keep old snapshot on failure
    }
  }

  async function fetchAgentLoopRunLog(loopId, cycle) {
    const requestedFor = { loopId, cycle }
    try {
      const res = await fetch(`/api/agentloops/${loopId}/runs/${cycle}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      if (selectedAgentLoopId.value !== requestedFor.loopId) return
      agentloopRunLog.value = { cycle, lines: data.lines || [] }
    } catch (e) {
      if (selectedAgentLoopId.value !== requestedFor.loopId) return
      agentloopRunLog.value = { cycle, lines: [] }
    }
  }

  async function selectAgentLoop(loopId) {
    selectedAgentLoopId.value = loopId
    currentTaskId.value = null
    agentloopSnapshot.value = null
    agentloopRunLog.value = { cycle: null, lines: [] }
    if (loopId) {
      await fetchAgentLoopSnapshot(loopId)
    }
  }

  function clearSelectedAgentLoop() {
    selectedAgentLoopId.value = null
    agentloopSnapshot.value = null
    agentloopRunLog.value = { cycle: null, lines: [] }
  }

  async function dismissAgentLoopRecent(loopId) {
    // optimistic UI update
    const idx = agentloops.value.findIndex(l => l.loop_id === loopId)
    if (idx !== -1) agentloops.value.splice(idx, 1)
    try {
      await fetch(`/api/agentloops/${loopId}/dismiss`, { method: 'POST' })
    } catch (e) {
      addToast(`Failed to dismiss agentloop: ${e.message}`, 'error')
    }
  }

  async function stopAgentLoop(loopId) {
    try {
      const res = await fetch(`/api/agentloops/${loopId}/stop`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await fetchAgentLoops()
      if (selectedAgentLoopId.value === loopId) {
        await fetchAgentLoopSnapshot(loopId)
      }
      addToast('AgentLoop 已停止', 'success')
    } catch (e) {
      addToast(`停止失败: ${e.message}`, 'error')
    }
  }

  // Re-launch a previously stopped / exhausted / done agentloop.
  // Backend POST /api/agentloops is idempotent: if status != running it respawns
  // on the same cwd/design, reading the existing .agentloop/state.json so work
  // resumes (e.g. another max_cycles worth of cycles after the prior run exited).
  async function startAgentLoop(loopId) {
    const entry = (agentloops.value || []).find(l => l.loop_id === loopId)
      || (selectedAgentLoopId.value === loopId && agentloopSnapshot.value)
      || null
    if (!entry || !entry.cwd) {
      addToast('无法启动：缺少 cwd 信息', 'error')
      return
    }
    try {
      const res = await fetch('/api/agentloops', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          cwd: entry.cwd,
          design_path: entry.design_path || null,
          source_task_id: entry.source_task_id || null,
          workspace: entry.workspace || null,
        }),
      })
      if (!res.ok) {
        const detail = await res.text().catch(() => '')
        throw new Error(`HTTP ${res.status}${detail ? `: ${detail}` : ''}`)
      }
      await fetchAgentLoops()
      if (selectedAgentLoopId.value === loopId) {
        await fetchAgentLoopSnapshot(loopId)
      }
      addToast('AgentLoop 已启动', 'success')
    } catch (e) {
      addToast(`启动失败: ${e.message}`, 'error')
    }
  }

  return {
    agents,
    tasks,
    taskSessions,
    currentTaskId,
    currentTask,
    collapsed,
    toasts,
    unseenTaskIds,
    memoryPanelOpen,
    memoryAgentId,
    agentMemory,
    promptsPanelOpen,
    recentFiles,
    addRecentFile,
    getRecentFiles,
    removeRecentFile,
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
    openPromptsPanel,
    closePromptsPanel,
    showArchived,
    archiveAgent,
    unarchiveAgent,
    setAgentMemory,
    prependMemoryEntry,
    removeMemoryEntry,
    upsertTask,
    replaceAgentTaskIds,
    updateTaskFields,
    updateTaskSession,
    addAgent,
    // agentloop
    selectedAgentLoopId,
    agentloops,
    agentloopSnapshot,
    agentloopRunLog,
    fetchAgentLoops,
    fetchAgentLoopSnapshot,
    fetchAgentLoopRunLog,
    selectAgentLoop,
    clearSelectedAgentLoop,
    dismissAgentLoopRecent,
    stopAgentLoop,
    startAgentLoop,
    findAgentLoopByCwd,
    findAgentLoopsByCwd,
  }
})
