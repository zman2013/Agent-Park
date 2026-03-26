<template>
  <div class="h-screen flex" @mousemove="onDrag" @mouseup="stopDrag" @mouseleave="stopDrag">
    <!-- Connection Status Bar -->
    <div
      v-if="!wsConnected"
      class="fixed top-0 left-0 right-0 z-50 bg-red-900/90 text-red-200 text-xs text-center py-1.5"
    >
      WebSocket disconnected — reconnecting...
    </div>

    <!-- Left Panel -->
    <template v-if="leftVisible">
      <template v-if="fileBrowserState.panelOpen">
        <div
          class="flex-shrink-0 bg-[#111] border-r border-gray-800 flex flex-col h-full overflow-hidden"
          :style="{ width: leftWidth + 'px' }"
        >
          <div class="p-4 flex items-center justify-between flex-shrink-0">
            <span class="text-xs text-gray-500 uppercase tracking-wider font-semibold">Files</span>
          </div>
          <div class="flex-shrink-0 px-2">
            <UnseenTasksPanel />
          </div>
          <FileBrowserPanel
            :agent-id="fileBrowserState.agentId"
            @close="closeFileBrowser"
            @file-select="onFileSelect"
          />
        </div>
      </template>
      <AgentTree v-else class="flex-shrink-0" :style="{ width: leftWidth + 'px' }" />

      <!-- Resize handle -->
      <div
        class="w-1 flex-shrink-0 cursor-col-resize hover:bg-blue-500/40 active:bg-blue-500/60 transition-colors"
        style="margin-left: -1px; z-index: 10;"
        @mousedown.prevent="startDrag"
      />
    </template>

    <!-- Right Panel -->
    <div class="flex-1 flex flex-col min-w-0 overflow-hidden">
      <template v-if="fileBrowserState.selectedFile">
        <FileContentView
          :agent-id="fileBrowserState.agentId"
          :file-path="fileBrowserState.selectedFile"
          :file-size="fileBrowserState.fileSize"
          @close="fileBrowserState.selectedFile = null"
        />
      </template>
      <template v-else-if="store.currentTask">
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
      <MemoryPanel
        :visible="store.memoryPanelOpen"
        :agent-id="store.memoryAgentId"
        :agent-name="memoryAgentName"
        @close="store.closeMemoryPanel()"
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
import MemoryPanel from './components/MemoryPanel.vue'
import FileBrowserPanel from './components/FileBrowserPanel.vue'
import FileContentView from './components/FileContentView.vue'
import UnseenTasksPanel from './components/UnseenTasksPanel.vue'

const store = useAgentStore()
const { connected: wsConnected, createTask, sendUserMessage } = useWebSocket()

const terminalVisible = ref(false)

// ── Left panel width & visibility ────────────────────────────────────────────
const LEFT_WIDTH_KEY = 'agent-park:left-width'
const LEFT_MIN = 180
const LEFT_MAX = 600
const LEFT_DEFAULT = 288 // w-72 = 18rem = 288px

const leftVisible = ref(true)
const leftWidth = ref(
  parseInt(localStorage.getItem(LEFT_WIDTH_KEY) || String(LEFT_DEFAULT), 10)
)

function saveWidth() {
  localStorage.setItem(LEFT_WIDTH_KEY, String(leftWidth.value))
}

// Drag state
let dragging = false
let dragStartX = 0
let dragStartWidth = 0

function startDrag(e) {
  dragging = true
  dragStartX = e.clientX
  dragStartWidth = leftWidth.value
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}

function onDrag(e) {
  if (!dragging) return
  const delta = e.clientX - dragStartX
  leftWidth.value = Math.min(LEFT_MAX, Math.max(LEFT_MIN, dragStartWidth + delta))
}

function stopDrag() {
  if (!dragging) return
  dragging = false
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
  saveWidth()
}

// ── File browser state ────────────────────────────────────────────────────────
const fileBrowserState = ref({
  panelOpen: false,
  agentId: null,
  selectedFile: null,
  fileSize: 0,
})

const currentAgentCwd = computed(() => {
  const task = store.currentTask
  if (!task) return ''
  const agent = store.agents.find(a => a.id === task.agent_id)
  return agent?.cwd || ''
})

const memoryAgentName = computed(() => {
  if (!store.memoryAgentId) return ''
  const agent = store.agents.find(a => a.id === store.memoryAgentId)
  return agent?.name || ''
})

function closeFileBrowser() {
  fileBrowserState.value.panelOpen = false
  fileBrowserState.value.agentId = null
  fileBrowserState.value.selectedFile = null
  fileBrowserState.value.fileSize = 0
}

function onFileSelect({ path, size }) {
  fileBrowserState.value.selectedFile = path
  fileBrowserState.value.fileSize = size
}

// ── Event handlers ────────────────────────────────────────────────────────────
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

function onOpenMemory(e) {
  store.openMemoryPanel(e.detail.agentId)
}

function onOpenFiles(e) {
  fileBrowserState.value.panelOpen = true
  fileBrowserState.value.agentId = e.detail.agentId
  fileBrowserState.value.selectedFile = null
  fileBrowserState.value.fileSize = 0
}

function handleGlobalKeydown(e) {
  if (e.metaKey && e.key === 'b') {
    e.preventDefault()
    leftVisible.value = !leftVisible.value
    return
  }
  if (e.metaKey && e.key === 'j') {
    e.preventDefault()
    terminalVisible.value = !terminalVisible.value
  }
  if (e.metaKey && e.key === 'k') {
    e.preventDefault()
    if (store.memoryPanelOpen) {
      store.closeMemoryPanel()
    } else {
      const task = store.currentTask
      const agentId = task?.agent_id || store.agents[0]?.id
      if (agentId) store.openMemoryPanel(agentId)
    }
  }
  if (e.key === 'Escape' && fileBrowserState.value.selectedFile) {
    fileBrowserState.value.selectedFile = null
  }
}

onMounted(() => {
  window.addEventListener('create-task', onCreateTask)
  window.addEventListener('send-message', onSendMessage)
  window.addEventListener('open-memory', onOpenMemory)
  window.addEventListener('open-files', onOpenFiles)
  window.addEventListener('keydown', handleGlobalKeydown)
})

onUnmounted(() => {
  window.removeEventListener('create-task', onCreateTask)
  window.removeEventListener('send-message', onSendMessage)
  window.removeEventListener('open-memory', onOpenMemory)
  window.removeEventListener('open-files', onOpenFiles)
  window.removeEventListener('keydown', handleGlobalKeydown)
})
</script>
