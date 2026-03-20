<template>
  <div class="border-t border-gray-800 p-3">
    <div
      v-if="task.status === 'waiting'"
      class="text-xs text-blue-400 mb-2 px-1"
    >
      Agent is waiting for your input...
    </div>
    <div class="flex gap-2 items-end">
      <div class="flex-1 flex flex-col">
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
      </div>
      <div class="flex gap-2">
        <select
          v-model="selectedCmd"
          class="bg-[#111] border border-gray-700 rounded-lg px-2 py-1 text-sm text-gray-300 focus:border-gray-500 outline-none"
        >
          <option v-for="cmd in availableCmds" :key="cmd" :value="cmd">{{ cmd }}</option>
        </select>
        <button
          class="bg-green-600 hover:bg-green-700 px-4 rounded-lg text-sm font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          :disabled="!text.trim()"
          @click="send"
        >
          Send
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick, computed } from 'vue'
import { useAgentStore } from '../stores/agentStore'

const props = defineProps({
  task: { type: Object, required: true },
})

const store = useAgentStore()
const emit = defineEmits(['send'])
const text = ref('')
const inputEl = ref(null)
const selectedCmd = ref('ccs')

// 硬编码可选的 agent cmd 列表
const availableCmds = ['ccs', 'cco', 'glm5', 'qwen']

// 根据当前任务关联的 Agent 设置默认选中的 cmd
const currentAgent = computed(() => {
  return store.agents.find(agent => agent.id === props.task.agent_id)
})

// 当任务或 Agent 列表变化时，更新选中的 cmd
const updateSelectedCmd = () => {
  if (currentAgent.value) {
    selectedCmd.value = currentAgent.value.command || 'ccs'
  } else {
    selectedCmd.value = 'ccs'
  }
}

// 监听 task 变化以更新 selectedCmd
updateSelectedCmd()

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
    detail: { taskId: props.task.id, content, command: selectedCmd.value }
  }))

  text.value = ''
  nextTick(() => autoResize())
}
</script>
