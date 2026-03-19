<template>
  <div class="flex flex-col flex-1 min-h-0">
    <!-- Turns indicator -->
    <div v-if="task.num_turns" class="flex items-center justify-end px-6 py-1 border-b border-gray-800 text-xs text-gray-500 shrink-0">
      <span>累计 <span class="text-gray-300 font-mono">{{ task.num_turns }}</span> turns</span>
    </div>
    <div ref="chatContainer" class="flex-1 overflow-auto p-6 space-y-3" @scroll="onScroll">
      <div v-if="task.messages.length === 0" class="text-gray-600 text-sm text-center mt-20">
        No messages yet. Send a prompt to get started.
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
import { ref, watch, nextTick, computed, onMounted } from 'vue'
import MessageBubble from './MessageBubble.vue'

const props = defineProps({
  task: { type: Object, required: true },
})

const visibleMessages = computed(() =>
  props.task.messages.filter(m => m.content || m.streaming)
)

const chatContainer = ref(null)
// Whether the user has not yet manually scrolled up after opening the window
const isAtBottom = ref(true)

function scrollToBottom() {
  const el = chatContainer.value
  if (el) {
    el.scrollTop = el.scrollHeight
  }
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
  async () => {
    if (!isAtBottom.value) return
    await nextTick()
    scrollToBottom()
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
  async () => {
    await nextTick()
    const el = chatContainer.value
    if (!el) return
    const threshold = 150
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < threshold
    if (nearBottom) {
      scrollToBottom()
    }
  }
)
</script>
