<template>
  <div class="flex flex-col flex-1 min-h-0">
    <!-- Combined session / turns / cost indicator -->
    <div class="flex items-center px-6 py-1 border-b border-gray-800 text-xs text-gray-600 shrink-0 font-mono overflow-hidden">
      <span v-if="sessionId" class="text-gray-700 mr-1">session:</span>
      <span v-if="sessionId" class="text-gray-500 select-all">{{ sessionId }}</span>
      <button
        v-if="matchedAgentLoop"
        @click="openAgentLoop"
        class="ml-3 text-blue-400 hover:text-blue-300 transition-colors"
        title="切换到 AgentLoop 面板"
      >查看 AgentLoop →</button>
      <span class="flex-1"></span>
      <template v-if="task.num_turns">
        <span class="text-gray-600">累计 <span class="text-gray-400">{{ task.num_turns }}</span> turns</span>
        <template v-if="task.model_usage && Object.keys(task.model_usage).length">
          <template v-for="(mu, model) in task.model_usage" :key="model">
            <span class="mx-2 text-gray-700">|</span>
            <span class="text-gray-500">{{ shortModel(model) }}</span>
            <span :class="ctxColor(mu)" class="ml-1">{{ ctxPct(mu) }}%</span>
            <span class="text-gray-700 ml-1">{{ fmtK(mu.inputTokens) }}/{{ fmtK(mu.outputTokens) }}</span>
          </template>
        </template>
        <template v-if="task.total_cost_cny">
          <span class="mx-2 text-gray-700">|</span>
          <span class="text-gray-500">¥{{ task.total_cost_cny.toFixed(2) }}</span>
        </template>
      </template>
      <button
        @click="copyConversation"
        class="ml-3 text-gray-600 hover:text-gray-300 transition-colors px-1"
        title="复制全部对话（排除工具交互）"
      >复制对话</button>
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
import { useAgentStore } from '../stores/agentStore'

const AUTO_SCROLL_DELAY_MS = 120
const MAX_LIVE_MESSAGES = 200
const MAX_COMPLETED_MESSAGES = 250

const props = defineProps({
  task: { type: Object, required: true },
})

const store = useAgentStore()

const sessionId = computed(() => store.taskSessions[props.task.id] || null)

// Match an active agentloop to the current task's agent cwd, if any.
const matchedAgentLoop = computed(() => {
  const agent = store.agents.find(a => a.id === props.task.agent_id)
  const cwd = agent?.cwd
  if (!cwd) return null
  return (store.agentloops || []).find(l => l.cwd === cwd) || null
})

function openAgentLoop() {
  if (matchedAgentLoop.value) {
    store.selectAgentLoop(matchedAgentLoop.value.loop_id)
  }
}

// Token usage display helpers
function shortModel(name) {
  // e.g. "claude-haiku-4-5-20251001" → "haiku", "sonnet-4.6[1m]" → "sonnet", "opus-4.6" → "opus"
  const m = name.match(/\b(haiku|sonnet|opus)\b/i)
  return m ? m[1].toLowerCase() : name.split('-')[0]
}
function ctxPct(mu) {
  const win = mu.contextWindow
  if (!win || !mu.inputTokens) return '0.0'
  return (mu.inputTokens / win * 100).toFixed(1)
}

function ctxColor(mu) {
  const pct = parseFloat(ctxPct(mu))
  if (pct >= 80) return 'text-red-400'
  if (pct >= 50) return 'text-yellow-400'
  return 'text-green-400'
}

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

const conversationText = computed(() => {
  const textMessages = allVisibleMessages.value.filter(m => m.type === 'text')
  return textMessages.map(m => {
    const label = m.role === 'user' ? 'User' : 'Agent'
    return `${label}:\n${m.content}`
  }).join('\n\n')
})

function copyToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text)
  }
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  const ok = document.execCommand('copy')
  document.body.removeChild(textarea)
  return ok ? Promise.resolve() : Promise.reject(new Error('execCommand failed'))
}

async function copyConversation() {
  const text = conversationText.value
  if (!text) {
    store.addToast('无对话内容', 'info')
    return
  }
  try {
    await copyToClipboard(text)
    store.addToast('已复制全部对话', 'success')
  } catch {
    store.addToast('复制失败', 'error')
  }
}
</script>
