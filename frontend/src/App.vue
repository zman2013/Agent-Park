<template>
  <div class="h-screen flex" @mousemove="onDrag" @mouseup="stopDrag" @mouseleave="stopDrag">
    <!-- Connection Status Bar -->
    <div
      v-if="!wsConnected"
      class="fixed top-0 left-0 right-0 z-50 bg-red-900/90 text-red-200 text-xs text-center py-1.5"
    >
      WebSocket disconnected — reconnecting...
    </div>

    <!-- Left Panel (AgentTree) -->
    <div v-if="leftVisible" class="flex-shrink-0 bg-[#111] flex flex-col h-full overflow-hidden" :style="{ width: leftWidth + 'px' }">
      <AgentTree class="flex-1 overflow-hidden" />
    </div>

    <!-- Resize handle: left ↔ center (shown when left is visible) -->
    <div
      v-if="leftVisible"
      class="w-1 flex-shrink-0 cursor-col-resize hover:bg-blue-500/40 active:bg-blue-500/60 transition-colors"
      style="margin-left: -1px; z-index: 10;"
      @mousedown.prevent="startLeftDrag($event)"
    />

    <!-- Center Panel -->
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
      </template>
      <div v-else class="flex-1 flex items-center justify-center text-gray-600 text-sm">
        Select a task or create a new one to get started
      </div>
      <ChatInput v-if="store.currentTask" :task="store.currentTask" />
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
      <PromptsPanel
        :visible="store.promptsPanelOpen"
        @close="store.closePromptsPanel()"
      />
    </div>

    <!-- Resize handle: center ↔ right (shown when right is visible) -->
    <div
      v-if="rightVisible"
      class="w-1 flex-shrink-0 cursor-col-resize hover:bg-blue-500/40 active:bg-blue-500/60 transition-colors"
      style="margin-left: -1px; z-index: 10;"
      @mousedown.prevent="startRightDrag($event)"
    />

    <!-- Right Panel (File Browser) -->
    <div v-if="rightVisible" class="flex-shrink-0 bg-[#111] border-l border-gray-800 flex flex-col h-full overflow-hidden" :style="{ width: rightWidth + 'px' }">
      <div class="p-4 flex items-center justify-between flex-shrink-0">
        <span class="text-xs text-gray-500 uppercase tracking-wider font-semibold">Files</span>
        <button class="text-gray-600 hover:text-gray-300 transition-colors text-lg leading-none" @click="closeFileBrowser" title="Close">×</button>
      </div>
      <div class="flex-shrink-0 px-2">
        <UnseenTasksPanel />
      </div>
      <FileBrowserPanel
        :agent-id="fileBrowserState.agentId"
        :initial-path="fileBrowserState.selectedFile || ''"
        @close="toggleFileBrowser"
        @file-select="onFileSelect"
      />
    </div>

    <!-- Command Palette -->
    <CommandPalette
      :visible="commandPaletteVisible"
      :mode="commandPaletteMode"
      :agent-id="currentAgentId"
      :agent-cwd="currentAgentCwd"
      @close="commandPaletteVisible = false"
      @execute-command="onCommandExecute"
      @open-file="onPaletteOpenFile"
      @open-directory="onPaletteOpenDirectory"
    />

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
import PromptsPanel from './components/PromptsPanel.vue'
import FileBrowserPanel from './components/FileBrowserPanel.vue'
import FileContentView from './components/FileContentView.vue'
import UnseenTasksPanel from './components/UnseenTasksPanel.vue'
import CommandPalette from './components/CommandPalette.vue'

const store = useAgentStore()
const { connected: wsConnected, createTask, sendUserMessage, forkTask } = useWebSocket()

const terminalVisible = ref(false)
const commandPaletteVisible = ref(false)
const commandPaletteMode = ref('command')

// ── Left panel width & visibility ────────────────────────────────────────────
const LEFT_WIDTH_KEY = 'agent-park:left-width'
const LEFT_MIN = 180
const LEFT_MAX = 600
const LEFT_DEFAULT = 288 // w-72 = 18rem = 288px

// ── Right panel width & visibility ──────────────────────────────────────────
const RIGHT_WIDTH_KEY = 'agent-park:right-width'
const RIGHT_MIN = 180
const RIGHT_MAX = 600
const RIGHT_DEFAULT = 288

const leftVisible = ref(true)
const leftWidth = ref(
  parseInt(localStorage.getItem(LEFT_WIDTH_KEY) || String(LEFT_DEFAULT), 10)
)
const rightVisible = ref(false)
const rightWidth = ref(
  parseInt(localStorage.getItem(RIGHT_WIDTH_KEY) || String(RIGHT_DEFAULT), 10)
)

function saveLeftWidth() {
  localStorage.setItem(LEFT_WIDTH_KEY, String(leftWidth.value))
}
function saveRightWidth() {
  localStorage.setItem(RIGHT_WIDTH_KEY, String(rightWidth.value))
}

// ── Drag state ───────────────────────────────────────────────────────────────
let dragging = 'none'  // 'none' | 'left' | 'right'
let dragStartX = 0
let dragStartDelta = 0

function startLeftDrag(e) {
  dragging = 'left'
  dragStartX = e.clientX
  dragStartDelta = leftWidth.value
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}

function startRightDrag(e) {
  dragging = 'right'
  dragStartX = e.clientX
  dragStartDelta = rightWidth.value
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}

function onDrag(e) {
  if (dragging === 'none') return
  const delta = e.clientX - dragStartX
  if (dragging === 'left') {
    leftWidth.value = Math.min(LEFT_MAX, Math.max(LEFT_MIN, dragStartDelta + delta))
  }
  if (dragging === 'right') {
    rightWidth.value = Math.min(RIGHT_MAX, Math.max(RIGHT_MIN, dragStartDelta - delta))
  }
}

function stopDrag() {
  if (dragging === 'none') return
  const wasLeft = dragging === 'left'
  const wasRight = dragging === 'right'
  dragging = 'none'
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
  if (wasLeft) saveLeftWidth()
  if (wasRight) saveRightWidth()
}

// ── File browser state ────────────────────────────────────────────────────────
const fileBrowserState = ref({
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

const currentAgentId = computed(() => {
  const task = store.currentTask
  if (!task) return null
  return task.agent_id
})

const memoryAgentName = computed(() => {
  if (!store.memoryAgentId) return ''
  const agent = store.agents.find(a => a.id === store.memoryAgentId)
  return agent?.name || ''
})

function closeFileBrowser() {
  rightVisible.value = false
  fileBrowserState.value.agentId = null
  fileBrowserState.value.selectedFile = null
  fileBrowserState.value.fileSize = 0
}

function toggleFileBrowser() {
  rightVisible.value = !rightVisible.value
}

function onFileSelect({ path, size }) {
  fileBrowserState.value.selectedFile = path
  fileBrowserState.value.fileSize = size
  if (fileBrowserState.value.agentId) {
    store.addRecentFile(fileBrowserState.value.agentId, path)
  }
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

function onForkTask(e) {
  const { taskId } = e.detail
  if (!wsConnected.value) {
    store.addToast('Cannot fork task: WebSocket not connected', 'error')
    return
  }
  forkTask(taskId)
}

function onOpenFiles(e) {
  rightVisible.value = true
  fileBrowserState.value.agentId = e.detail.agentId
  fileBrowserState.value.selectedFile = null
  fileBrowserState.value.fileSize = 0
}

// ── Command Palette handlers ─────────────────────────────────────────────────
function onCommandExecute(commandId) {
  switch (commandId) {
    case 'toggle-sidebar':
      leftVisible.value = !leftVisible.value
      break
    case 'toggle-terminal':
      terminalVisible.value = !terminalVisible.value
      break
    case 'toggle-memory': {
      if (store.memoryPanelOpen) {
        store.closeMemoryPanel()
      } else {
        const agentId = currentAgentId.value || store.agents[0]?.id
        if (agentId) store.openMemoryPanel(agentId)
      }
      break
    }
    case 'toggle-prompts':
      if (store.promptsPanelOpen) {
        store.closePromptsPanel()
      } else {
        store.openPromptsPanel()
      }
      break
    case 'toggle-file-browser': {
      toggleFileBrowser()
      break
    }
    case 'open-files': {
      const agentId = currentAgentId.value
      if (agentId) {
        rightVisible.value = true
        fileBrowserState.value.agentId = agentId
        fileBrowserState.value.selectedFile = null
        fileBrowserState.value.fileSize = 0
      }
      break
    }
    case 'create-task': {
      const agentId = currentAgentId.value || store.agents[0]?.id
      if (agentId && wsConnected.value) {
        createTask(agentId, '')
      }
      break
    }
  }
}

function onPaletteOpenFile({ agentId, path, size }) {
  rightVisible.value = true
  fileBrowserState.value.agentId = agentId
  fileBrowserState.value.selectedFile = path
  fileBrowserState.value.fileSize = size
  store.addRecentFile(agentId, path)
}

function onPaletteOpenDirectory({ agentId, path }) {
  rightVisible.value = true
  fileBrowserState.value.agentId = agentId
  fileBrowserState.value.selectedFile = null
  fileBrowserState.value.fileSize = 0
}

function handleGlobalKeydown(e) {
  // ⌘⇧P — command palette (command mode)
  if (e.metaKey && e.shiftKey && (e.key === 'p' || e.key === 'P')) {
    e.preventDefault()
    commandPaletteMode.value = 'command'
    commandPaletteVisible.value = true
    return
  }
  // ⌘P — command palette (file mode)
  if (e.metaKey && !e.shiftKey && e.key === 'p') {
    e.preventDefault()
    commandPaletteMode.value = 'file'
    commandPaletteVisible.value = true
    return
  }
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
  if (e.metaKey && e.key === 'u') {
    e.preventDefault()
    if (store.promptsPanelOpen) {
      store.closePromptsPanel()
    } else {
      store.openPromptsPanel()
    }
  }
  if (e.metaKey && e.key === 'i') {
    e.preventDefault()
    toggleFileBrowser()
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
  window.addEventListener('fork-task', onForkTask)
  window.addEventListener('keydown', handleGlobalKeydown)
})

onUnmounted(() => {
  window.removeEventListener('create-task', onCreateTask)
  window.removeEventListener('send-message', onSendMessage)
  window.removeEventListener('open-memory', onOpenMemory)
  window.removeEventListener('open-files', onOpenFiles)
  window.removeEventListener('fork-task', onForkTask)
  window.removeEventListener('keydown', handleGlobalKeydown)
})
</script>
