<template>
  <Transition name="terminal-slide">
    <div
      v-if="visible"
      class="flex flex-col border-t border-gray-700 bg-[#0d0d0d]"
      style="height: 50%"
    >
      <!-- Title bar -->
      <div class="flex items-center justify-between px-3 py-1.5 border-b border-gray-800 shrink-0 select-none bg-[#111]">
        <div class="flex items-center gap-2 text-xs text-gray-500 min-w-0">
          <span class="text-green-500 shrink-0">$</span>
          <span class="font-mono text-gray-400 truncate" :title="cwd">{{ cwd || '~' }}</span>
        </div>
        <div class="flex items-center gap-3 shrink-0 ml-2">
          <span class="text-xs text-gray-600">⌘J</span>
          <button
            class="text-gray-600 hover:text-gray-300 text-xs px-1 leading-none"
            @click="$emit('close')"
          >✕</button>
        </div>
      </div>

      <!-- Output area -->
      <div ref="outputEl" class="flex-1 overflow-auto p-3 font-mono text-xs leading-5">
        <div v-for="(entry, i) in history" :key="i" class="mb-2">
          <!-- Command line -->
          <div v-if="!entry.isMeta" class="flex items-start gap-1 text-green-400">
            <span class="text-gray-600 shrink-0 select-none">{{ shortCwd }}</span>
            <span class="text-green-500 shrink-0 select-none">$</span>
            <span>{{ entry.command }}</span>
          </div>
          <div v-else class="text-gray-600 italic">{{ entry.command }}</div>
          <!-- Output -->
          <pre v-if="entry.output" class="whitespace-pre-wrap text-gray-300 mt-0.5 ml-0">{{ entry.output }}</pre>
        </div>
        <!-- Empty state -->
        <div v-if="history.length === 0" class="text-gray-600 text-xs">
          Terminal ready. Type a command below.
        </div>
      </div>

      <!-- Input line -->
      <div class="border-t border-gray-800 px-3 py-2 shrink-0 flex items-center gap-2 bg-[#111]">
        <span class="text-gray-600 font-mono text-xs shrink-0 select-none">{{ shortCwd }}</span>
        <span class="text-green-500 font-mono text-xs shrink-0 select-none">$</span>
        <input
          ref="inputEl"
          v-model="inputText"
          class="flex-1 bg-transparent outline-none text-xs font-mono text-gray-200 caret-green-400 min-w-0"
          placeholder="type a command..."
          :disabled="isRunning"
          @keydown="handleKeydown"
        />
        <span v-if="isRunning" class="text-yellow-500 text-xs shrink-0 select-none">running...</span>
      </div>
    </div>
  </Transition>
</template>

<script setup>
import { ref, computed, watch, nextTick, reactive } from 'vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  cwd: { type: String, default: '' },
})

const emit = defineEmits(['close'])

const history = ref([])
const cmdHistory = ref([])
const historyIndex = ref(-1)
const inputText = ref('')
const inputEl = ref(null)
const outputEl = ref(null)
const isRunning = ref(false)
let abortController = null

const shortCwd = computed(() => {
  const cwd = props.cwd
  if (!cwd) return '~'
  const parts = cwd.replace(/\/$/, '').split('/')
  if (parts.length <= 2) return cwd
  return parts.slice(-2).join('/')
})

watch(() => props.visible, (v) => {
  if (v) nextTick(() => inputEl.value?.focus())
})

watch(() => props.cwd, (newCwd, oldCwd) => {
  if (newCwd !== oldCwd && history.value.length > 0) {
    history.value.push({ command: `[cwd changed → ${newCwd || '~'}]`, output: '', isMeta: true })
    nextTick(scrollToBottom)
  }
})

function handleKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    runCommand()
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    navigateHistory(1)
  } else if (e.key === 'ArrowDown') {
    e.preventDefault()
    navigateHistory(-1)
  } else if (e.key === 'c' && e.ctrlKey) {
    e.preventDefault()
    abortCurrent()
  }
}

function navigateHistory(dir) {
  // dir=1: older (up arrow), dir=-1: newer (down arrow)
  const len = cmdHistory.value.length
  if (len === 0) return
  const newIdx = historyIndex.value + dir
  if (newIdx >= len) {
    historyIndex.value = len - 1
    inputText.value = cmdHistory.value[0]
  } else if (newIdx < 0) {
    historyIndex.value = -1
    inputText.value = ''
  } else {
    historyIndex.value = newIdx
    // cmdHistory is oldest-first; index 0 = oldest, last = newest
    // newIdx=0 → newest, newIdx=len-1 → oldest
    inputText.value = cmdHistory.value[len - 1 - newIdx]
  }
}

function abortCurrent() {
  if (abortController) {
    abortController.abort()
    abortController = null
  }
}

async function runCommand() {
  const cmd = inputText.value.trim()
  if (!cmd || isRunning.value) return

  inputText.value = ''
  historyIndex.value = -1
  cmdHistory.value.push(cmd)

  const entry = reactive({ command: cmd, output: '', running: true, isMeta: false })
  history.value.push(entry)
  isRunning.value = true
  abortController = new AbortController()

  await nextTick()
  scrollToBottom()

  try {
    const response = await fetch('/api/shell/exec', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cwd: props.cwd, command: cmd }),
      signal: abortController.signal,
    })

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      entry.output += decoder.decode(value, { stream: true })
      scrollToBottom()
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      entry.output += (entry.output ? '\n' : '') + '^C'
    } else {
      entry.output += (entry.output ? '\n' : '') + `Error: ${err.message}`
    }
  } finally {
    entry.running = false
    isRunning.value = false
    abortController = null
    await nextTick()
    scrollToBottom()
    inputEl.value?.focus()
  }
}

function scrollToBottom() {
  nextTick(() => {
    if (outputEl.value) {
      outputEl.value.scrollTop = outputEl.value.scrollHeight
    }
  })
}
</script>
