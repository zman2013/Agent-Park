<template>
  <div class="border-t border-gray-800 p-3">
    <div
      v-if="task.status === 'waiting'"
      class="text-xs text-blue-400 mb-2 px-1"
    >
      Agent is waiting for your input...
    </div>
    <div class="flex gap-2">
      <textarea
        ref="inputEl"
        v-model="text"
        class="flex-1 bg-[#111] border border-gray-700 p-2.5 rounded-lg outline-none text-sm resize-none focus:border-gray-500 transition-colors"
        :class="{ 'border-blue-500/50': task.status === 'waiting' }"
        placeholder="Reply to agent..."
        rows="1"
        @keydown="handleKeydown"
        @input="autoResize"
      ></textarea>
      <button
        class="bg-green-600 hover:bg-green-700 px-4 rounded-lg text-sm font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        :disabled="!text.trim()"
        @click="send"
      >
        Send
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'

const props = defineProps({
  task: { type: Object, required: true },
})

const emit = defineEmits(['send'])
const text = ref('')
const inputEl = ref(null)

function handleKeydown(e) {
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

function send() {
  const content = text.value.trim()
  if (!content) return

  window.dispatchEvent(new CustomEvent('send-message', {
    detail: { taskId: props.task.id, content }
  }))

  text.value = ''
  nextTick(() => autoResize())
}
</script>
