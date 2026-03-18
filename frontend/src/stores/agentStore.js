import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAgentStore = defineStore('agent', () => {
  const agents = ref([])
  const tasks = ref({})
  const currentTaskId = ref(null)
  const collapsed = ref({})
  const toasts = ref([])

  const currentTask = computed(() => {
    if (!currentTaskId.value) return null
    return tasks.value[currentTaskId.value] || null
  })

  function syncState(data) {
    agents.value = data.agents || []
    // Merge tasks to preserve reactivity
    const newTasks = {}
    for (const [id, task] of Object.entries(data.tasks || {})) {
      newTasks[id] = task
    }
    tasks.value = newTasks
  }

  function updateTaskStatus(taskId, status) {
    const task = tasks.value[taskId]
    if (task) {
      task.status = status
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
  }

  function updateAgent(agentId, fields) {
    const agent = agents.value.find(a => a.id === agentId)
    if (agent) {
      Object.assign(agent, fields)
    }
  }

  return {
    agents,
    tasks,
    currentTaskId,
    currentTask,
    collapsed,
    toasts,
    syncState,
    updateTaskStatus,
    addMessage,
    appendChunk,
    markMessageDone,
    selectTask,
    toggleAgent,
    isCollapsed,
    addToast,
    removeToast,
    removeTask,
    updateAgent,
  }
})
