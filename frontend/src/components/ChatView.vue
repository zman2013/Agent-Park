<template>
  <div class="flex flex-col flex-1 min-h-0">
    <!-- Turns indicator -->
    <div v-if="task.num_turns" class="flex items-center justify-end px-6 py-1 border-b border-gray-800 text-xs text-gray-500 shrink-0">
      <span>累计 <span class="text-gray-300 font-mono">{{ task.num_turns }}</span> turns</span>
      <template v-if="task.total_input_tokens">
        <span class="mx-2 text-gray-700">|</span>
        <span :class="ctxColor" class="font-mono">CTX:{{ ctxPct }}%</span>
        <span class="text-gray-600 font-mono ml-1">(in:{{ fmtK(task.total_input_tokens) }} out:{{ fmtK(task.total_output_tokens) }})</span>
        <template v-if="task.total_cost_cny">
          <span class="mx-2 text-gray-700">|</span>
          <span class="text-gray-400 font-mono">¥{{ task.total_cost_cny.toFixed(2) }}</span>
        </template>
      </template>
    </div>
    <div ref="chatContainer" class="flex-1 overflow-auto p-6 space-y-3" @scroll="onScroll">
      <div v-if="task.messages.length === 0" class="text-gray-600 text-sm text-center mt-20">
        No messages yet. Send a prompt to get started.
      </div>
      <div v-if="isLiveWindowing" class="text-gray-600 text-xs text-center">
        Showing the latest {{ renderedMessageCount }} messages during live streaming for performance.
      </div>
      <div v-else-if="isCompletedWindowing" class="flex items-center justify-center gap-3 text-gray-600 text-xs text-center">
        <span>Showing the latest {{ renderedMessageCount }} of {{ allVisibleMessages.length }} messages.</span>
        <button class="text-blue-400 hover:text-blue-300" @click="showAllHistory = true">Load full history</button>
      </div>
      <div v-else-if="canToggleHistoryWindow" class="flex items-center justify-center gap-3 text-gray-600 text-xs text-center">
        <span>Showing all {{ renderedMessageCount }} messages.</span>
        <button class="text-blue-400 hover:text-blue-300" @click="showAllHistory = false">Show latest only</button>
      </div>
      <MessageBubble
        v-for="msg in visibleMessages"
        :key="msg.id"
        :message="msg"
      />
      <div v-if="task.status === 'running'" class="streaming-cursor text-sm px-4"></div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick, computed, onMounted, onUnmounted } from 'vue'
import MessageBubble from './MessageBubble.vue'

const AUTO_SCROLL_DELAY_MS = 120
const MAX_LIVE_MESSAGES = 200
const MAX_COMPLETED_MESSAGES = 250

const props = defineProps({
  task: { type: Object, required: true },
})

// Token usage display helpers
const ctxPct = computed(() => {
  const win = props.task.context_window
  if (!win || !props.task.total_input_tokens) return '0.0'
  return (props.task.total_input_tokens / win * 100).toFixed(1)
})

const ctxColor = computed(() => {
  const pct = parseFloat(ctxPct.value)
  if (pct >= 80) return 'text-red-400'
  if (pct >= 50) return 'text-yellow-400'
  return 'text-green-400'
})

function fmtK(n) {
  if (!n) return '0'
  return Math.round(n / 1000) + 'k'
}

const chatContainer = ref(null)
// Whether the user has not yet manually scrolled up after opening the window
const isAtBottom = ref(true)
const showAllHistory = ref(false)
let autoScrollTimer = null

const allVisibleMessages = computed(() =>
  props.task.messages.filter(m => m.content || m.streaming)
)

const isLiveWindowing = computed(() =>
  props.task.status === 'running' &&
  isAtBottom.value &&
  allVisibleMessages.value.length > MAX_LIVE_MESSAGES
)

const hasLargeCompletedHistory = computed(() =>
  props.task.status !== 'running' &&
  allVisibleMessages.value.length > MAX_COMPLETED_MESSAGES
)

const isCompletedWindowing = computed(() =>
  hasLargeCompletedHistory.value && !showAllHistory.value
)

const canToggleHistoryWindow = computed(() =>
  hasLargeCompletedHistory.value && showAllHistory.value
)

const visibleMessages = computed(() => {
  if (isLiveWindowing.value) {
    return allVisibleMessages.value.slice(-MAX_LIVE_MESSAGES)
  }
  if (isCompletedWindowing.value) {
    return allVisibleMessages.value.slice(-MAX_COMPLETED_MESSAGES)
  }
  return allVisibleMessages.value
})

const renderedMessageCount = computed(() => visibleMessages.value.length)

function scrollToBottom() {
  const el = chatContainer.value
  if (el) {
    el.scrollTop = el.scrollHeight
  }
}

function clearAutoScrollTimer() {
  if (autoScrollTimer) {
    clearTimeout(autoScrollTimer)
    autoScrollTimer = null
  }
}

function scheduleAutoScroll(force = false) {
  clearAutoScrollTimer()
  autoScrollTimer = setTimeout(async () => {
    autoScrollTimer = null
    await nextTick()
    const el = chatContainer.value
    if (!el) return
    if (!force) {
      const threshold = 150
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < threshold
      if (!nearBottom) return
    }
    scrollToBottom()
  }, AUTO_SCROLL_DELAY_MS)
}

function onScroll() {
  const el = chatContainer.value
  if (!el) return
  const threshold = 150
  isAtBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < threshold
}

// Scroll to bottom and reset state when switching tasks (first open)
watch(
  () => props.task?.id,
  async () => {
    isAtBottom.value = true
    showAllHistory.value = false
    await nextTick()
    scrollToBottom()
  }
)

// Scroll to bottom on initial mount
onMounted(async () => {
  await nextTick()
  scrollToBottom()
})

// On new messages: only auto-scroll if user is still at the bottom
watch(
  () => props.task.messages.length,
  () => {
    if (!isAtBottom.value) return
    scheduleAutoScroll(true)
  }
)

// Watch for streaming content changes, only scroll if near the bottom
watch(
  () => {
    const msgs = props.task.messages
    if (msgs.length === 0) return ''
    const last = msgs[msgs.length - 1]
    return last.content?.length || 0
  },
  () => {
    scheduleAutoScroll()
  }
)

onUnmounted(() => {
  clearAutoScrollTimer()
})
</script>
