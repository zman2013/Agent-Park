<template>
  <div class="border-t border-gray-800 p-3 relative">
    <!-- Top action bar -->
    <div class="flex items-center gap-2 mb-2">
      <button
        class="text-xs px-2 py-0.5 rounded border transition-colors"
        :class="isAutoCompactDisabled
          ? 'border-orange-600/60 text-orange-400 hover:border-orange-500 hover:text-orange-300'
          : 'border-gray-700 text-gray-500 hover:border-gray-500 hover:text-gray-300'"
        :title="isAutoCompactDisabled ? '点击开启自动压缩' : '点击关闭自动压缩'"
        @click="toggleAutoCompact"
      >
        {{ isAutoCompactDisabled ? '自动压缩：关' : '自动压缩：开' }}
      </button>
      <button
        class="text-xs px-2 py-0.5 rounded border border-gray-700 text-gray-500 hover:border-yellow-600/60 hover:text-yellow-400 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        :disabled="task.status === 'running'"
        title="立即压缩上下文"
        @click="compactNow"
      >
        开始压缩
      </button>
      <button
        class="text-xs px-2 py-0.5 rounded border border-gray-700 text-gray-500 hover:border-blue-600/60 hover:text-blue-400 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        :disabled="task.status === 'running' || !canSyncRemote"
        :title="canSyncRemote ? '同步远端代码到本地（stash → fetch → rebase → stash pop）' : '需要配置 agent 工作目录'"
        @click="syncRemote"
      >
        同步远端代码
      </button>
    </div>

    <div
      v-if="task.status === 'waiting'"
      class="text-xs text-blue-400 mb-2 px-1"
    >
      Agent is waiting for your input...
    </div>

    <!-- Prompt context checkboxes -->
    <div v-if="promptContexts.length > 0" class="flex flex-wrap gap-x-3 gap-y-1 mb-2 px-1">
      <label
        v-for="ctx in promptContexts"
        :key="ctx.id"
        class="flex items-center gap-1 cursor-pointer select-none"
      >
        <input
          type="checkbox"
          class="w-3 h-3 accent-gray-500 cursor-pointer"
          :checked="checkedContexts.has(ctx.id)"
          @change="toggleContext(ctx.id)"
        />
        <span class="text-xs text-gray-500 hover:text-gray-400 transition-colors">{{ ctx.label }}</span>
      </label>
    </div>

    <!-- Skill autocomplete dropdown -->
    <div
      v-if="showSkillMenu && filteredSkills.length > 0"
      class="absolute bottom-full left-3 right-3 mb-1 bg-[#1a1a1a] border border-gray-700 rounded-lg overflow-hidden shadow-lg z-10 max-h-64 overflow-y-auto"
    >
      <div
        v-for="(skill, idx) in filteredSkills"
        :key="skill.name"
        class="flex items-baseline gap-2 px-3 py-2 cursor-pointer hover:bg-gray-800 transition-colors"
        :class="{ 'bg-gray-800': idx === activeIndex }"
        @mousedown.prevent="selectSkill(skill)"
      >
        <span class="text-blue-400 font-mono text-sm shrink-0">/{{ skill.name }}</span>
        <span class="text-gray-400 text-xs truncate">{{ skill.description }}</span>
      </div>
    </div>

    <ChatAttachments :attachments="attachments" @remove="removeAttachment" />

    <input
      ref="fileInputEl"
      type="file"
      multiple
      class="hidden"
      @change="onFilesChosen"
    />

    <div class="flex gap-2 items-stretch">
      <button
        class="bg-[#1a1a1a] border border-gray-700 hover:border-gray-500 hover:text-gray-100 text-gray-300 w-10 h-10 self-end rounded-lg text-lg leading-none transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
        :disabled="!canUpload"
        :title="uploadTitle"
        @click="triggerFileChooser"
      >+</button>
      <textarea
        ref="inputEl"
        v-model="text"
        class="flex-1 bg-[#111] border border-gray-700 p-2.5 rounded-lg outline-none text-sm resize-none focus:border-gray-500 transition-colors"
        :class="{ 'border-blue-500/50': task.status === 'waiting' }"
        placeholder="Reply to agent..."
        rows="1"
        @keydown="handleKeydown"
        @input="handleInput"
        @blur="handleBlur"
        @paste="handlePaste"
      ></textarea>
      <button
        class="bg-green-600 hover:bg-green-700 px-4 rounded-lg text-sm font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
        :disabled="!canSend"
        @click="send"
      >
        Send
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { useAgentStore } from '../stores/agentStore.js'
import ChatAttachments from './ChatAttachments.vue'

const props = defineProps({
  task: { type: Object, required: true },
})

const emit = defineEmits(['send'])
const text = ref('')
const inputEl = ref(null)
const fileInputEl = ref(null)
const store = useAgentStore()

// Skill autocomplete state
const allSkills = ref([])
const showSkillMenu = ref(false)
const activeIndex = ref(0)

// Attachments state — not persisted across reloads (File objects + physical files)
const attachments = ref([])

// Prompt context checkboxes
const promptContexts = ref([])
const checkedContexts = ref(new Set())

const CONTEXT_STORAGE_KEY = 'prompt_context_checked'
const CONTEXT_SEEN_KEY = 'prompt_context_seen'

function loadContextState() {
  try {
    const saved = localStorage.getItem(CONTEXT_STORAGE_KEY)
    const seen = localStorage.getItem(CONTEXT_SEEN_KEY)
    if (saved !== null) {
      return {
        checked: new Set(JSON.parse(saved)),
        seen: seen ? new Set(JSON.parse(seen)) : null,
      }
    }
  } catch {}
  return null
}

function saveContextState() {
  const ids = promptContexts.value.map(c => c.id)
  localStorage.setItem(CONTEXT_STORAGE_KEY, JSON.stringify([...checkedContexts.value]))
  localStorage.setItem(CONTEXT_SEEN_KEY, JSON.stringify(ids))
}

function toggleContext(id) {
  if (checkedContexts.value.has(id)) {
    checkedContexts.value.delete(id)
  } else {
    checkedContexts.value.add(id)
  }
  saveContextState()
}

async function fetchPromptContexts() {
  try {
    const res = await fetch('/api/prompt-contexts')
    if (!res.ok) return
    const list = await res.json()
    promptContexts.value = list
    if (!list.length) return
    const state = loadContextState()
    if (state !== null) {
      const knownIds = new Set(list.map(c => c.id))
      // Keep checked ids that still exist in current list
      const preserved = new Set([...state.checked].filter(id => knownIds.has(id)))
      // Only auto-check defaults for ids that were never seen before (truly new)
      const seenIds = state.seen ?? new Set()
      const newDefaults = list.filter(c => c.default && !seenIds.has(c.id)).map(c => c.id)
      checkedContexts.value = new Set([...preserved, ...newDefaults])
    } else {
      checkedContexts.value = new Set(list.filter(c => c.default).map(c => c.id))
    }
    saveContextState()
  } catch {}
}

const DRAFT_KEY = 'chat_draft'

// Debounce timer for saving draft
let saveTimer = null
function saveDraft(val) {
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    if (val) {
      localStorage.setItem(DRAFT_KEY, val)
    } else {
      localStorage.removeItem(DRAFT_KEY)
    }
  }, 500)
}

// Watch text changes and persist with debounce
watch(text, (val) => {
  saveDraft(val)
})

// Reset attachments when task switches — App.vue reuses ChatInput across
// tasks, so without this the next send would attach files uploaded under
// the previous task (and DELETE would be issued against the wrong agent).
watch(() => props.task.id, (_newId, oldId) => {
  if (oldId === undefined) return
  discardAttachments()
})

const agent = computed(() => store.agents.find(a => a.id === props.task.agent_id))
const canUpload = computed(() => !!agent.value?.cwd)
const canSyncRemote = computed(() => !!agent.value?.cwd)
const isAutoCompactDisabled = computed(() => !!store.autoCompactDisabled[props.task.id])
const uploadTitle = computed(() =>
  canUpload.value ? 'Upload files' : 'Configure agent cwd to enable uploads'
)
const hasUploading = computed(() =>
  attachments.value.some(a => a.status === 'uploading')
)
const canSend = computed(() => {
  if (hasUploading.value) return false
  if (text.value.trim()) return true
  return attachments.value.some(a => a.status === 'success')
})

onMounted(async () => {
  // Restore draft from localStorage
  const saved = localStorage.getItem(DRAFT_KEY)
  if (saved) {
    text.value = saved
    nextTick(() => autoResize())
  }

  // Auto-focus input when mounted (e.g. after task creation)
  nextTick(() => inputEl.value?.focus())

  window.addEventListener('fill-prompt', onFillPrompt)

  try {
    const cwd = agent.value?.cwd || ''
    const url = cwd ? `/api/skills?cwd=${encodeURIComponent(cwd)}` : '/api/skills'
    const res = await fetch(url)
    if (res.ok) {
      allSkills.value = await res.json()
    }
  } catch {
    // ignore
  }

  fetchPromptContexts()
})

onUnmounted(() => {
  window.removeEventListener('fill-prompt', onFillPrompt)
  // Abort any in-flight uploads but leave persisted files alone — the
  // user can clean them up later via the filesystem if desired.
  for (const att of attachments.value) {
    if (att.status === 'uploading' && att.xhr) {
      try { att.xhr.abort() } catch {}
    }
  }
})

function onFillPrompt(e) {
  text.value = e.detail.content
  nextTick(() => {
    autoResize()
    inputEl.value?.focus()
  })
}

const filteredSkills = computed(() => {
  if (!showSkillMenu.value) return []
  const query = text.value
  if (!query.startsWith('/') && !query.startsWith('、')) return []
  const prefix = query.slice(1).toLowerCase()
  if (prefix === '') return allSkills.value
  return allSkills.value.filter(s => s.name.toLowerCase().startsWith(prefix))
})

function isSkillTrigger(val) {
  return (val.startsWith('/') || val.startsWith('、')) && !val.includes(' ')
}

function handleInput() {
  autoResize()
  const val = text.value
  if (isSkillTrigger(val)) {
    showSkillMenu.value = true
    activeIndex.value = 0
  } else {
    showSkillMenu.value = false
  }
}

function handleBlur() {
  // Delay to allow mousedown on dropdown items to fire first
  setTimeout(() => {
    showSkillMenu.value = false
  }, 150)
}

function selectSkill(skill) {
  text.value = '/' + skill.name + ' '
  showSkillMenu.value = false
  nextTick(() => {
    autoResize()
    inputEl.value?.focus()
  })
}

function handleKeydown(e) {
  if (showSkillMenu.value && filteredSkills.value.length > 0) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      activeIndex.value = (activeIndex.value + 1) % filteredSkills.value.length
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      activeIndex.value = (activeIndex.value - 1 + filteredSkills.value.length) % filteredSkills.value.length
      return
    }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
      e.preventDefault()
      selectSkill(filteredSkills.value[activeIndex.value])
      return
    }
    if (e.key === 'Escape') {
      showSkillMenu.value = false
      return
    }
  }

  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

function autoResize() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 150) + 'px'
}

function triggerFileChooser() {
  if (!canUpload.value) return
  fileInputEl.value?.click()
}

function onFilesChosen(e) {
  const files = Array.from(e.target.files || [])
  for (const f of files) {
    startUpload(f)
  }
  // Reset so picking the same file again retriggers change
  e.target.value = ''
}

function handlePaste(e) {
  // Only intercept when the clipboard actually carries image files.
  // Plain text paste must keep the browser default behavior.
  if (!canUpload.value) return
  const items = e.clipboardData?.items || []
  const imageFiles = []
  let hasText = false
  for (const it of items) {
    if (it.kind === 'file' && it.type.startsWith('image/')) {
      const f = it.getAsFile()
      if (f) imageFiles.push(f)
    } else if (it.kind === 'string') {
      // Any string item counts as text — text/plain, text/html, text/uri-list,
      // rich-text payloads, etc. We must not preventDefault when any of these
      // are present, otherwise the browser drops them silently.
      hasText = true
    }
  }
  if (imageFiles.length === 0) return
  // Only suppress the default paste path when the clipboard is image-only.
  // For mixed image+text clipboards, let the browser insert the text and
  // we still pick up the image files for upload.
  if (!hasText) {
    e.preventDefault()
  }
  for (const f of imageFiles) {
    startUpload(normalizePastedImage(f))
  }
}

function normalizePastedImage(file) {
  // Browsers hand pasted screenshots a generic name like "image.png" (or
  // sometimes empty). Multiple pastes would collide visually in the chip
  // list, so synthesize a timestamped name. Backend still uuid-prefixes
  // the stored filename.
  const generic = !file.name || file.name === 'image.png' || file.name === 'image'
  if (!generic) return file
  const subtype = (file.type.split('/')[1] || 'png').toLowerCase()
  const ext = subtype === 'jpeg' ? 'jpg' : subtype
  const ts = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14)
  return new File([file], `pasted-${ts}.${ext}`, { type: file.type })
}

function startUpload(file) {
  const uid = Math.random().toString(36).slice(2, 10)
  const agentId = props.task.agent_id
  attachments.value.push({
    uid,
    name: file.name,
    size: file.size,
    status: 'uploading',
    progress: 0,
    absPath: null,
    relPath: null,
    error: null,
    xhr: null,
    agentId,
  })
  // Re-fetch the reactive proxy so handler writes trigger UI updates.
  // Plain `push` keeps the closure's reference as the raw object, which
  // bypasses the reactive proxy and leaves the UI stuck on "uploading".
  const att = attachments.value[attachments.value.length - 1]

  const xhr = new XMLHttpRequest()
  att.xhr = xhr
  const form = new FormData()
  form.append('file', file)
  xhr.open('POST', `/api/agents/${agentId}/uploads`)
  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable) {
      att.progress = Math.round((ev.loaded / ev.total) * 100)
    }
  }
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      try {
        const data = JSON.parse(xhr.responseText)
        att.status = 'success'
        att.absPath = data.abs_path
        att.relPath = data.rel_path
        att.size = data.size
        att.progress = 100
        att.xhr = null
      } catch (err) {
        att.status = 'failed'
        att.error = 'invalid server response'
      }
    } else {
      att.status = 'failed'
      let msg = `HTTP ${xhr.status}`
      try {
        const data = JSON.parse(xhr.responseText)
        if (data?.detail) msg = data.detail
      } catch {}
      att.error = msg
      store.addToast(`Upload failed: ${file.name} — ${msg}`, 'error')
    }
  }
  xhr.onerror = () => {
    att.status = 'failed'
    att.error = 'network error'
    store.addToast(`Upload failed: ${file.name}`, 'error')
  }
  xhr.onabort = () => {
    // Removed by user during upload — nothing to do here, removeAttachment
    // already handled the array splice.
  }
  xhr.send(form)
}

function removeAttachment(uid) {
  const idx = attachments.value.findIndex(a => a.uid === uid)
  if (idx === -1) return
  const att = attachments.value[idx]
  attachments.value.splice(idx, 1)

  if (att.status === 'uploading') {
    if (att.xhr) {
      try { att.xhr.abort() } catch {}
    }
    return
  }
  if (att.status === 'success' && att.relPath) {
    const url = `/api/agents/${att.agentId}/uploads?rel_path=${encodeURIComponent(att.relPath)}`
    fetch(url, { method: 'DELETE' }).catch(() => {
      // Best-effort: even on failure the UI entry is gone; user can clean
      // up the .agent-park/uploads directory manually if needed.
    })
  }
}

function discardAttachments() {
  // Drop every attachment, aborting in-flight uploads and best-effort DELETE
  // for completed ones. Each attachment carries the agent_id it was uploaded
  // under, so this works correctly across task switches.
  const snapshot = attachments.value.slice()
  attachments.value = []
  for (const att of snapshot) {
    if (att.status === 'uploading' && att.xhr) {
      try { att.xhr.abort() } catch {}
    } else if (att.status === 'success' && att.relPath) {
      const url = `/api/agents/${att.agentId}/uploads?rel_path=${encodeURIComponent(att.relPath)}`
      fetch(url, { method: 'DELETE' }).catch(() => {})
    }
  }
}

function toggleAutoCompact() {
  const newDisabled = !isAutoCompactDisabled.value
  window.dispatchEvent(new CustomEvent('toggle-auto-compact', {
    detail: { taskId: props.task.id, disabled: newDisabled }
  }))
}

function compactNow() {
  if (props.task.status === 'running') return
  window.dispatchEvent(new CustomEvent('trigger-compact', {
    detail: { taskId: props.task.id }
  }))
}

function syncRemote() {
  if (props.task.status === 'running' || !canSyncRemote.value) return
  const ts = new Date().toISOString().replace(/[^0-9]/g, '').slice(0, 14)
  const content = `请帮我同步远端代码到本地。步骤：
1. 先执行 \`git status --porcelain\` 检查是否有本地改动。
2. 若有改动（包括 tracked 和 untracked 文件），用 \`git stash push --include-untracked -m "sync-remote-${ts}"\` 创建具名 stash 保存所有改动，记下这个 stash name。
3. 执行 \`git fetch\` 然后 \`git rebase origin/<当前分支>\` 同步远端最新代码。
4. 若第 2 步创建了 stash，用 \`git stash pop stash@{0}\`（或通过 stash name 定位）恢复改动；若未创建 stash 则跳过此步。
5. 如有冲突请协助解决。
注意：只 pop 本次操作创建的 stash，不要 pop 其他已有 stash。`
  const evt = new CustomEvent('send-message', {
    cancelable: true,
    detail: { taskId: props.task.id, content },
  })
  window.dispatchEvent(evt)
}

function send() {
  if (hasUploading.value) {
    store.addToast('请等待文件上传完成', 'warning')
    return
  }
  const trimmed = text.value.trim()
  const ready = attachments.value.filter(a => a.status === 'success')
  if (!trimmed && ready.length === 0) return

  let content = trimmed
  if (ready.length > 0) {
    const lines = ready.map((a, i) => `${i + 1}. ${a.absPath}`).join('\n')
    content = (trimmed ? `${trimmed}\n\n` : '') + `附件列表：\n${lines}`
  }

  const activeContexts = promptContexts.value.filter(c => checkedContexts.value.has(c.id))
  if (activeContexts.length > 0) {
    const paths = activeContexts.map(c => `- ${c.path}`).join('\n')
    content = content + `\n\n---\nBackground context (read if relevant):\n${paths}`
  }

  const evt = new CustomEvent('send-message', {
    cancelable: true,
    detail: { taskId: props.task.id, content },
  })
  const accepted = window.dispatchEvent(evt)
  if (!accepted) {
    // App.vue rejected the send (e.g. WebSocket disconnected). Keep the
    // text and attachments staged so the user can retry without losing
    // their uploads.
    return
  }

  text.value = ''
  attachments.value = []
  clearTimeout(saveTimer)
  localStorage.removeItem(DRAFT_KEY)
  showSkillMenu.value = false
  nextTick(() => autoResize())
}
</script>
