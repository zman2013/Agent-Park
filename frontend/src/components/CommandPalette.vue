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
              <!-- Recent files (only when no query) -->
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

              <!-- Search results (when query is non-empty) -->
              <template v-if="fileQuery">
                <div v-if="searchLoading" class="px-3 py-4 text-xs text-gray-600 text-center">Searching...</div>
                <div v-else-if="searchError" class="px-3 py-4 text-xs text-red-400 text-center">{{ searchError }}</div>
                <!-- Path resolve result (single result for path-like queries) -->
                <template v-else-if="pathResolveResult">
                  <div
                    v-if="pathResolveResult.exists"
                    :class="[
                      'flex items-center px-3 py-1.5 cursor-pointer text-sm transition-colors',
                      activeIndex === 0 ? 'bg-blue-900/30 text-gray-100' : 'text-gray-400 hover:bg-gray-800/60'
                    ]"
                    @click="selectResolvedPath(pathResolveResult)"
                    @mouseenter="activeIndex = 0"
                  >
                    <span class="text-gray-500 mr-2 flex-shrink-0">{{ pathResolveResult.type === 'dir' ? '📁' : fileIcon(pathResolveResult.path.split('/').pop()) }}</span>
                    <span class="truncate min-w-0" style="flex: 0 1 auto">{{ pathResolveResult.path.split('/').pop() }}{{ pathResolveResult.type === 'dir' ? '/' : '' }}</span>
                    <span class="text-gray-600 text-xs ml-2 flex-shrink-0 truncate max-w-[240px] font-mono" :title="pathResolveResult.path">
                      {{ shortenPath(pathResolveResult.path) }}
                    </span>
                    <span class="flex-1"></span>
                    <span v-if="pathResolveResult.type === 'file' && pathResolveResult.size != null" class="text-gray-600 text-xs ml-2 flex-shrink-0 tabular-nums">
                      {{ formatSize(pathResolveResult.size) }}
                    </span>
                  </div>
                  <div v-else class="px-3 py-4 text-xs text-gray-600 text-center">
                    <span class="text-gray-500">Path not found:</span>
                    <span class="text-gray-400 ml-1 font-mono">{{ pathResolveResult.path || fileQuery }}</span>
                    <span v-if="pathResolveResult.error" class="block text-red-400 mt-1">{{ pathResolveResult.error }}</span>
                  </div>
                </template>
                <!-- Fuzzy search results -->
                <template v-else-if="searchResults.length > 0">
                  <div
                    v-for="(item, i) in searchResults"
                    :key="'search-' + item.path"
                    :class="[
                      'flex items-center px-3 py-1.5 cursor-pointer text-sm transition-colors',
                      i === activeIndex ? 'bg-blue-900/30 text-gray-100' : 'text-gray-400 hover:bg-gray-800/60'
                    ]"
                    @click="selectSearchResult(item)"
                    @mouseenter="activeIndex = i"
                  >
                    <span class="text-gray-500 mr-2 flex-shrink-0">{{ item.type === 'dir' ? '📁' : fileIcon(item.name) }}</span>
                    <span class="truncate min-w-0" style="flex: 0 1 auto">{{ item.name }}{{ item.type === 'dir' ? '/' : '' }}</span>
                    <span class="text-gray-600 text-xs ml-2 flex-shrink-0 truncate max-w-[240px] font-mono" :title="item.path">
                      {{ shortenPath(item.path) }}
                    </span>
                    <span class="flex-1"></span>
                    <span v-if="item.type === 'file' && item.size != null" class="text-gray-600 text-xs ml-2 flex-shrink-0 tabular-nums">
                      {{ formatSize(item.size) }}
                    </span>
                  </div>
                </template>
                <div v-else class="px-3 py-4 text-xs text-gray-600 text-center">
                  No matching files
                </div>
              </template>

              <!-- Directory listing (when no query) -->
              <template v-if="!fileQuery">
                <div v-if="dirLoading" class="px-3 py-4 text-xs text-gray-600 text-center">Loading...</div>
                <div v-else-if="dirError" class="px-3 py-4 text-xs text-red-400 text-center">{{ dirError }}</div>
                <template v-else>
                  <div v-if="currentDirPath" class="px-3 pt-1.5 pb-1 text-gray-600 text-xs font-mono truncate select-none" :title="fullCurrentPath">
                    {{ fullCurrentPath }}
                  </div>
                  <div
                    v-for="(entry, i) in dirEntries"
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
                  <div v-if="dirEntries.length === 0 && !dirLoading" class="px-3 py-4 text-xs text-gray-600 text-center">
                    Empty directory
                  </div>
                </template>
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
import { ref, computed, watch, nextTick, onUnmounted } from 'vue'
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

// Search state
const searchResults = ref([])
const searchLoading = ref(false)
const searchError = ref('')
let searchTimer = null
let searchAbort = null  // AbortController for in-flight request

// ── Command definitions ─────────────────────────────────────────────────────
const commands = [
  { id: 'toggle-sidebar', label: 'Toggle Sidebar', shortcut: '⌘B', keywords: ['sidebar', 'left', 'panel'] },
  { id: 'toggle-terminal', label: 'Toggle Terminal', shortcut: '⌘J', keywords: ['terminal', 'shell'] },
  { id: 'toggle-memory', label: 'Toggle Memory Panel', shortcut: '⌘K', keywords: ['memory', 'context'] },
  { id: 'toggle-prompts', label: 'Toggle Prompts Panel', shortcut: '⌘U', keywords: ['prompts'] },
  { id: 'toggle-file-browser', label: 'Toggle File Browser', shortcut: '⌘L', keywords: ['files', 'explorer', 'browser', 'right'] },
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
const fileQueryRaw = computed(() => query.value.trim())

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
  // If currentDirPath is already absolute (e.g. from symlink target), use it directly
  if (currentDirPath.value.startsWith('/')) return currentDirPath.value
  return base + '/' + currentDirPath.value
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

// Navigate to a directory given an absolute or relative path string
async function navigateToDir(dirPath) {
  currentDirPath.value = dirPath
  await loadDir(dirPath)
}

// ── File mode: recursive search ─────────────────────────────────────────────
async function searchFiles(q) {
  if (!props.agentId || !q) return
  // Cancel previous in-flight request
  if (searchAbort) searchAbort.abort()
  const controller = new AbortController()
  searchAbort = controller

  searchLoading.value = true
  searchError.value = ''
  try {
    const url = `/api/agents/${props.agentId}/files/search?q=${encodeURIComponent(q)}&limit=50`
    const res = await fetch(url, { signal: controller.signal })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      throw new Error(data.detail || `HTTP ${res.status}`)
    }
    const data = await res.json()
    searchResults.value = data.results || []
  } catch (e) {
    if (e.name === 'AbortError') return  // superseded by newer request
    searchError.value = e.message
    searchResults.value = []
  } finally {
    if (searchAbort === controller) {
      searchLoading.value = false
      searchAbort = null
    }
  }
}

// ── File mode: path resolve (for absolute/relative paths) ────────────────────
const pathResolveResult = ref(null)  // null | { exists: boolean, ... }

function looksLikePath(q) {
  // Starts with / or ./ or contains / (likely a path, not a search query)
  if (!q) return false
  return q.startsWith('/') || q.startsWith('./') || q.includes('/')
}

async function resolvePath(path) {
  if (!props.agentId || !path) return
  // Cancel previous in-flight request
  if (searchAbort) searchAbort.abort()
  const controller = new AbortController()
  searchAbort = controller

  searchLoading.value = true
  searchError.value = ''
  pathResolveResult.value = null

  try {
    const url = `/api/agents/${props.agentId}/files/resolve?path=${encodeURIComponent(path)}`
    const res = await fetch(url, { signal: controller.signal })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      throw new Error(data.detail || `HTTP ${res.status}`)
    }
    const data = await res.json()
    pathResolveResult.value = data
  } catch (e) {
    if (e.name === 'AbortError') return
    searchError.value = e.message
    pathResolveResult.value = null
  } finally {
    if (searchAbort === controller) {
      searchLoading.value = false
      searchAbort = null
    }
  }
}

function cancelSearch() {
  if (searchTimer) { clearTimeout(searchTimer); searchTimer = null }
  if (searchAbort) { searchAbort.abort(); searchAbort = null }
  searchResults.value = []
  searchLoading.value = false
  searchError.value = ''
  pathResolveResult.value = null
}

// Watch fileQuery to debounce search or resolve path
watch(fileQuery, (q) => {
  if (isCommandMode.value) return
  cancelSearch()
  if (!q) return  // empty query → show directory listing, no search

  // If input looks like a path, use resolve API (preserve original case for path resolution)
  if (looksLikePath(fileQueryRaw.value)) {
    searchTimer = setTimeout(() => resolvePath(fileQueryRaw.value), 150)
  } else {
    searchTimer = setTimeout(() => searchFiles(q), 250)
  }
})

onUnmounted(() => {
  cancelSearch()
})

// ── Item count for keyboard nav ─────────────────────────────────────────────
const totalItems = computed(() => {
  if (isCommandMode.value) {
    return taskListMode.value ? filteredTaskItems.value.length : filteredCommands.value.length
  }
  if (fileQuery.value) {
    // Path resolve returns single result
    if (pathResolveResult.value) {
      return pathResolveResult.value.exists ? 1 : 0
    }
    return searchResults.value.length
  }
  return dirListOffset.value + dirEntries.value.length
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
    cancelSearch()
    if (props.mode === 'file' && props.agentId) {
      loadDir('')
    }
    nextTick(() => inputEl.value?.focus())
  } else {
    cancelSearch()
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
    if (fileQuery.value) {
      // Path resolve result (single result)
      if (pathResolveResult.value && pathResolveResult.value.exists) {
        selectResolvedPath(pathResolveResult.value)
      } else {
        // Search results mode
        const item = searchResults.value[activeIndex.value]
        if (item) selectSearchResult(item)
      }
    } else {
      const recentIdx = activeIndex.value
      if (recentIdx < recentFilesList.value.length) {
        selectFile(recentFilesList.value[recentIdx])
      } else {
        const dirIdx = activeIndex.value - dirListOffset.value
        const entry = dirEntries.value[dirIdx]
        if (entry) selectEntry(entry)
      }
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
    navigateToDir(newPath)
    query.value = ''
    activeIndex.value = 0
  } else {
    const filePath = currentDirPath.value ? currentDirPath.value + '/' + entry.name : entry.name
    store.addRecentFile(props.agentId, filePath)
    emit('open-file', { agentId: props.agentId, path: filePath, size: entry.size || 0 })
    emit('close')
  }
}

function selectSearchResult(item) {
  if (item.type === 'dir') {
    // Navigate into directory, clear search
    navigateToDir(item.path)
    query.value = ''
    activeIndex.value = 0
    cancelSearch()
  } else {
    store.addRecentFile(props.agentId, item.path)
    emit('open-file', { agentId: props.agentId, path: item.path, size: item.size || 0 })
    emit('close')
  }
}

function selectResolvedPath(result) {
  if (result.type === 'dir') {
    // Navigate into directory, clear search
    navigateToDir(result.path)
    query.value = ''
    activeIndex.value = 0
    cancelSearch()
  } else {
    store.addRecentFile(props.agentId, result.path)
    emit('open-file', { agentId: props.agentId, path: result.path, size: result.size || 0 })
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
