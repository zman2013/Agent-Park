<template>
  <Teleport to="body">
    <Transition name="palette">
      <div
        v-if="visible"
        class="fixed inset-0 z-[60] flex justify-center"
        style="padding-top: 18vh"
        @mousedown.self="$emit('close')"
      >
        <div
          class="w-full max-w-xl bg-[#1a1a1a] border border-gray-700 rounded-xl shadow-2xl flex flex-col"
          style="max-height: 480px"
        >
          <!-- Input -->
          <div class="flex items-center border-b border-gray-700 px-3">
            <span v-if="isCommandMode" class="text-gray-500 text-sm mr-1 select-none">&gt;</span>
            <input
              ref="inputEl"
              :value="isCommandMode ? stripCommandPrefix(query) : query"
              class="flex-1 bg-transparent text-gray-200 text-sm py-3 outline-none placeholder-gray-600"
              :placeholder="isCommandMode ? 'Type a command...' : 'Search files by name...'"
              @input="onInput"
              @keydown="onKeydown"
            />
          </div>

          <!-- List -->
          <div ref="listEl" class="flex-1 overflow-auto py-1 min-h-0">
            <!-- Command mode -->
            <template v-if="isCommandMode">
              <div
                v-for="(cmd, i) in filteredCommands"
                :key="cmd.id"
                :class="[
                  'flex items-center px-3 py-1.5 cursor-pointer text-sm transition-colors',
                  i === activeIndex ? 'bg-blue-900/30 text-gray-100' : 'text-gray-400 hover:bg-gray-800/60'
                ]"
                @click="executeCommand(cmd)"
                @mouseenter="activeIndex = i"
              >
                <span class="flex-1 truncate">{{ cmd.label }}</span>
                <span v-if="cmd.shortcut" class="text-gray-600 text-xs font-mono ml-3 flex-shrink-0">{{ cmd.shortcut }}</span>
              </div>
              <div v-if="filteredCommands.length === 0" class="px-3 py-4 text-xs text-gray-600 text-center">
                No matching commands
              </div>
            </template>

            <!-- File navigation mode -->
            <template v-else>
              <!-- Recent files -->
              <template v-if="!fileQuery && recentFilesList.length > 0">
                <div class="px-3 pt-1.5 pb-1 text-gray-600 text-xs uppercase tracking-wider select-none">
                  recently opened
                </div>
                <div
                  v-for="(file, i) in recentFilesList"
                  :key="'recent-' + file.path"
                  :class="[
                    'flex items-center px-3 py-1.5 cursor-pointer text-sm transition-colors',
                    i === activeIndex ? 'bg-blue-900/30 text-gray-100' : 'text-gray-400 hover:bg-gray-800/60'
                  ]"
                  @click="selectFile(file)"
                  @mouseenter="activeIndex = i"
                >
                  <span class="text-gray-500 mr-2 flex-shrink-0">📄</span>
                  <span class="truncate flex-1 min-w-0">{{ file.name }}</span>
                  <span class="text-gray-600 text-xs ml-2 flex-shrink-0 truncate max-w-[200px]" :title="file.path">
                    {{ shortenPath(file.path) }}
                  </span>
                </div>
                <div class="border-t border-gray-800 mx-2 my-1"></div>
              </template>

              <!-- Directory entries -->
              <div v-if="dirLoading" class="px-3 py-4 text-xs text-gray-600 text-center">Loading...</div>
              <div v-else-if="dirError" class="px-3 py-4 text-xs text-red-400 text-center">{{ dirError }}</div>
              <template v-else>
                <div v-if="currentDirPath" class="px-3 pt-1.5 pb-1 text-gray-600 text-xs font-mono truncate select-none" :title="fullCurrentPath">
                  {{ fullCurrentPath }}
                </div>
                <div
                  v-for="(entry, i) in filteredDirEntries"
                  :key="'dir-' + entry.name"
                  :class="[
                    'flex items-center px-3 py-1.5 cursor-pointer text-sm transition-colors',
                    (i + dirListOffset) === activeIndex ? 'bg-blue-900/30 text-gray-100' : 'text-gray-400 hover:bg-gray-800/60'
                  ]"
                  @click="selectEntry(entry)"
                  @mouseenter="activeIndex = i + dirListOffset"
                >
                  <span class="text-gray-500 mr-2 flex-shrink-0">{{ entry.type === 'dir' ? '📁' : fileIcon(entry.name) }}</span>
                  <span class="truncate flex-1 min-w-0">{{ entry.name }}{{ entry.type === 'dir' ? '/' : '' }}</span>
                  <span v-if="entry.type === 'file' && entry.size != null" class="text-gray-600 text-xs ml-2 flex-shrink-0 tabular-nums">
                    {{ formatSize(entry.size) }}
                  </span>
                </div>
                <div v-if="filteredDirEntries.length === 0 && !dirLoading" class="px-3 py-4 text-xs text-gray-600 text-center">
                  {{ fileQuery ? 'No matching files' : 'Empty directory' }}
                </div>
              </template>

              <!-- No agent hint -->
              <div v-if="!agentId" class="px-3 py-6 text-xs text-gray-600 text-center">
                Select a task to browse files
              </div>
            </template>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import { useAgentStore } from '../stores/agentStore'

const props = defineProps({
  visible: Boolean,
  mode: { type: String, default: 'command' },   // 'command' | 'file'
  agentId: { type: String, default: null },
  agentCwd: { type: String, default: '' },
})

const emit = defineEmits(['close', 'execute-command', 'open-file', 'open-directory'])

const store = useAgentStore()

const inputEl = ref(null)
const listEl = ref(null)
const query = ref('')
const activeIndex = ref(0)
const currentDirPath = ref('')
const dirEntries = ref([])
const dirLoading = ref(false)
const dirError = ref('')
const dirCwd = ref('')

// ── Command definitions ─────────────────────────────────────────────────────
const commands = [
  { id: 'toggle-sidebar', label: 'Toggle Sidebar', shortcut: '⌘B', keywords: ['sidebar', 'left', 'panel'] },
  { id: 'toggle-terminal', label: 'Toggle Terminal', shortcut: '⌘J', keywords: ['terminal', 'shell'] },
  { id: 'toggle-memory', label: 'Toggle Memory Panel', shortcut: '⌘K', keywords: ['memory', 'context'] },
  { id: 'toggle-prompts', label: 'Toggle Prompts Panel', shortcut: '⌘U', keywords: ['prompts'] },
  { id: 'go-to-task', label: 'Go to Task...', shortcut: '', keywords: ['task', 'switch', 'jump'] },
  { id: 'open-files', label: 'Open Files', shortcut: '', keywords: ['files', 'explorer', 'browser'] },
  { id: 'create-task', label: 'Create New Task', shortcut: '', keywords: ['new', 'task', 'create'] },
]

// ── Mode detection ──────────────────────────────────────────────────────────
const CMD_PREFIXES = ['>', '》']
function hasCommandPrefix(str) {
  return CMD_PREFIXES.some(p => str.startsWith(p))
}
function stripCommandPrefix(str) {
  for (const p of CMD_PREFIXES) {
    if (str.startsWith(p)) return str.slice(p.length)
  }
  return str
}
const isCommandMode = computed(() => hasCommandPrefix(query.value))
const commandQuery = computed(() => stripCommandPrefix(query.value).trim().toLowerCase())
const fileQuery = computed(() => query.value.trim().toLowerCase())

// ── Command filtering ───────────────────────────────────────────────────────
const filteredCommands = computed(() => {
  const q = commandQuery.value
  if (!q) return commands
  return commands.filter(cmd => {
    const haystack = (cmd.label + ' ' + cmd.keywords.join(' ')).toLowerCase()
    // fuzzy: every char in q appears in order
    let hi = 0
    for (let qi = 0; qi < q.length; qi++) {
      hi = haystack.indexOf(q[qi], hi)
      if (hi === -1) return false
      hi++
    }
    return true
  })
})

// ── Go to Task sub-mode ─────────────────────────────────────────────────────
const taskListMode = ref(false)
const taskItems = computed(() => {
  const items = []
  for (const agent of store.agents) {
    for (const tid of agent.task_ids) {
      const task = store.tasks[tid]
      if (task) {
        items.push({ id: task.id, label: task.name || task.prompt?.slice(0, 60) || tid, agentName: agent.name, agentId: agent.id })
      }
    }
  }
  return items
})

const filteredTaskItems = computed(() => {
  const q = commandQuery.value
  if (!q) return taskItems.value
  return taskItems.value.filter(t => {
    const haystack = (t.label + ' ' + t.agentName).toLowerCase()
    return haystack.includes(q)
  })
})

// ── File mode: recent files ─────────────────────────────────────────────────
const recentFilesList = computed(() => {
  if (!props.agentId) return []
  return store.getRecentFiles(props.agentId).slice(0, 10)
})

// Offset so directory entries indices come after recent files
const dirListOffset = computed(() => {
  if (fileQuery.value || recentFilesList.value.length === 0) return 0
  return recentFilesList.value.length
})

// ── File mode: directory listing ────────────────────────────────────────────
const fullCurrentPath = computed(() => {
  const base = dirCwd.value || props.agentCwd || ''
  if (!currentDirPath.value) return base
  return base + '/' + currentDirPath.value
})

const filteredDirEntries = computed(() => {
  const q = fileQuery.value
  if (!q) return dirEntries.value
  return dirEntries.value.filter(e => e.name.toLowerCase().includes(q))
})

async function loadDir(subPath = '') {
  if (!props.agentId) return
  dirLoading.value = true
  dirError.value = ''
  try {
    const url = `/api/agents/${props.agentId}/files` + (subPath ? `?path=${encodeURIComponent(subPath)}` : '')
    const res = await fetch(url)
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      throw new Error(data.detail || `HTTP ${res.status}`)
    }
    const data = await res.json()
    dirCwd.value = data.cwd
    dirEntries.value = data.entries
    currentDirPath.value = data.path || ''
  } catch (e) {
    dirError.value = e.message
    dirEntries.value = []
  } finally {
    dirLoading.value = false
  }
}

// ── Item count for keyboard nav ─────────────────────────────────────────────
const totalItems = computed(() => {
  if (isCommandMode.value) {
    return taskListMode.value ? filteredTaskItems.value.length : filteredCommands.value.length
  }
  return dirListOffset.value + filteredDirEntries.value.length
})

// ── Open / close logic ──────────────────────────────────────────────────────
watch(() => props.visible, (v) => {
  if (v) {
    query.value = props.mode === 'command' ? '>' : ''
    activeIndex.value = 0
    taskListMode.value = false
    currentDirPath.value = ''
    dirEntries.value = []
    dirError.value = ''
    if (props.mode === 'file' && props.agentId) {
      loadDir('')
    }
    nextTick(() => inputEl.value?.focus())
  }
})

// Watch mode switch via > prefix
watch(isCommandMode, (isCmdNow, wasCmdBefore) => {
  activeIndex.value = 0
  taskListMode.value = false
  if (!isCmdNow && props.agentId) {
    // Switched to file mode, load dir if not loaded
    if (dirEntries.value.length === 0 && !dirLoading.value) {
      loadDir(currentDirPath.value)
    }
  }
})

// ── Input handling ──────────────────────────────────────────────────────────
function onInput(e) {
  const raw = e.target.value
  if (isCommandMode.value) {
    // Already in command mode — re-prepend canonical '>'
    query.value = '>' + raw
  } else if (hasCommandPrefix(raw)) {
    // User typed '>' or '》' to enter command mode — normalize to '>'
    query.value = '>' + stripCommandPrefix(raw)
  } else {
    query.value = raw
  }
  activeIndex.value = 0
}

function onKeydown(e) {
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('close')
    return
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    if (totalItems.value > 0) {
      activeIndex.value = (activeIndex.value + 1) % totalItems.value
      scrollToActive()
    }
    return
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault()
    if (totalItems.value > 0) {
      activeIndex.value = (activeIndex.value - 1 + totalItems.value) % totalItems.value
      scrollToActive()
    }
    return
  }
  if (e.key === 'Enter') {
    e.preventDefault()
    onEnter()
    return
  }
  if (e.key === 'Backspace' && isCommandMode.value && stripCommandPrefix(query.value) === '') {
    e.preventDefault()
    // Exit command mode back to file mode
    query.value = ''
    activeIndex.value = 0
    if (props.agentId && dirEntries.value.length === 0 && !dirLoading.value) {
      loadDir(currentDirPath.value)
    }
    return
  }
  if (e.key === 'Backspace' && !isCommandMode.value && !query.value && currentDirPath.value) {
    e.preventDefault()
    // Go up one directory
    const parts = currentDirPath.value.split('/')
    parts.pop()
    const parentPath = parts.join('/')
    currentDirPath.value = parentPath
    loadDir(parentPath)
    activeIndex.value = 0
  }
}

function onEnter() {
  if (isCommandMode.value) {
    if (taskListMode.value) {
      const item = filteredTaskItems.value[activeIndex.value]
      if (item) {
        store.selectTask(item.id)
        emit('close')
      }
      return
    }
    const cmd = filteredCommands.value[activeIndex.value]
    if (cmd) executeCommand(cmd)
  } else {
    // File mode
    const recentIdx = activeIndex.value
    if (!fileQuery.value && recentIdx < recentFilesList.value.length) {
      selectFile(recentFilesList.value[recentIdx])
    } else {
      const dirIdx = activeIndex.value - dirListOffset.value
      const entry = filteredDirEntries.value[dirIdx]
      if (entry) selectEntry(entry)
    }
  }
}

function executeCommand(cmd) {
  if (cmd.id === 'go-to-task') {
    taskListMode.value = true
    activeIndex.value = 0
    // Keep palette open, switch to task list
    return
  }
  emit('execute-command', cmd.id)
  emit('close')
}

function selectFile(file) {
  store.addRecentFile(props.agentId, file.path)
  emit('open-file', { agentId: props.agentId, path: file.path, size: file.size || 0 })
  emit('close')
}

function selectEntry(entry) {
  if (entry.type === 'dir') {
    const newPath = currentDirPath.value ? currentDirPath.value + '/' + entry.name : entry.name
    currentDirPath.value = newPath
    query.value = ''
    activeIndex.value = 0
    loadDir(newPath)
  } else {
    const filePath = currentDirPath.value ? currentDirPath.value + '/' + entry.name : entry.name
    store.addRecentFile(props.agentId, filePath)
    emit('open-file', { agentId: props.agentId, path: filePath, size: entry.size || 0 })
    emit('close')
  }
}

function scrollToActive() {
  nextTick(() => {
    const el = listEl.value?.querySelector('.bg-blue-900\\/30')
    if (el) el.scrollIntoView({ block: 'nearest' })
  })
}

// ── Utilities ───────────────────────────────────────────────────────────────
const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp', '.avif'])

function fileIcon(name) {
  const ext = name.includes('.') ? name.slice(name.lastIndexOf('.')).toLowerCase() : ''
  if (IMAGE_EXTS.has(ext)) return '🖼'
  return '📄'
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function shortenPath(fullPath) {
  const parts = fullPath.split('/')
  if (parts.length <= 3) return fullPath
  return '.../' + parts.slice(-2).join('/')
}
</script>
