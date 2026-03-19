<template>
  <div class="h-screen flex">
    <!-- Connection Status Bar -->
    <div
      v-if="!wsConnected"
      class="fixed top-0 left-0 right-0 z-50 bg-red-900/90 text-red-200 text-xs text-center py-1.5"
    >
      WebSocket disconnected — reconnecting...
    </div>

    <!-- Left Panel -->
    <AgentTree class="w-72 flex-shrink-0" />

    <!-- Right Panel -->
    <div class="flex-1 flex flex-col min-w-0 overflow-hidden">
      <template v-if="store.currentTask">
        <ChatView :task="store.currentTask" />
        <ChatInput :task="store.currentTask" />
      </template>
      <div v-else class="flex-1 flex items-center justify-center text-gray-600 text-sm">
        Select a task or create a new one to get started
      </div>
      <TerminalPanel
        :visible="terminalVisible"
        :cwd="currentAgentCwd"
        @close="terminalVisible = false"
      />
    </div>

    <!-- Toasts -->
    <ToastContainer />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useAgentStore } from './stores/agentStore'
import { useWebSocket } from './composables/useWebSocket'
import AgentTree from './components/AgentTree.vue'
import ChatView from './components/ChatView.vue'
import ChatInput from './components/ChatInput.vue'
import ToastContainer from './components/ToastContainer.vue'
import TerminalPanel from './components/TerminalPanel.vue'

const store = useAgentStore()
const { connected: wsConnected, createTask, sendUserMessage } = useWebSocket()

const terminalVisible = ref(false)

const currentAgentCwd = computed(() => {
  const task = store.currentTask
  if (!task) return ''
  const agent = store.agents.find(a => a.id === task.agent_id)
  return agent?.cwd || ''
})

function onCreateTask(e) {
  const { agentId, name } = e.detail
  if (!wsConnected.value) {
    store.addToast('Cannot create task: WebSocket not connected', 'error')
    return
  }
  createTask(agentId, name)
}

function onSendMessage(e) {
  const { taskId, content } = e.detail
  if (!wsConnected.value) {
    store.addToast('Cannot send message: WebSocket not connected', 'error')
    return
  }
  sendUserMessage(taskId, content)
}

function handleGlobalKeydown(e) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'j') {
    e.preventDefault()
    terminalVisible.value = !terminalVisible.value
  }
}

onMounted(() => {
  window.addEventListener('create-task', onCreateTask)
  window.addEventListener('send-message', onSendMessage)
  window.addEventListener('keydown', handleGlobalKeydown)
})

onUnmounted(() => {
  window.removeEventListener('create-task', onCreateTask)
  window.removeEventListener('send-message', onSendMessage)
  window.removeEventListener('keydown', handleGlobalKeydown)
})
</script>
