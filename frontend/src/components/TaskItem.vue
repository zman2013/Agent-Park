<template>
  <div
    v-if="task"
    class="flex items-center gap-2 px-4 py-1.5 cursor-pointer rounded text-sm transition-colors"
    :class="isActive ? 'bg-gray-800 text-gray-100' : 'hover:bg-gray-800/50 text-gray-400'"
    @click="store.selectTask(task.id)"
  >
    <span class="text-xs" :class="statusClass">{{ statusIcon }}</span>
    <span class="truncate flex-1">{{ task.name || 'Untitled' }}</span>
    <button
      class="text-gray-600 hover:text-red-400 text-xs opacity-0 group-hover:opacity-100 transition-opacity"
      @click.stop="handleDelete"
      title="Delete task"
    >
      ×
    </button>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useAgentStore } from '../stores/agentStore'

const props = defineProps({
  task: { type: Object, required: true },
})

const store = useAgentStore()
const isActive = computed(() => store.currentTaskId === props.task?.id)

const statusIcon = computed(() => {
  switch (props.task?.status) {
    case 'running': return '●'
    case 'waiting': return '●'
    case 'success': return '●'
    case 'failed': return '●'
    default: return '○'
  }
})

const statusClass = computed(() => {
  switch (props.task?.status) {
    case 'running': return 'text-yellow-400 status-running'
    case 'waiting': return 'text-blue-400 status-running'
    case 'success': return 'text-green-500'
    case 'failed': return 'text-red-500'
    default: return 'text-gray-600'
  }
})

async function handleDelete() {
  try {
    await fetch(`/api/tasks/${props.task.id}`, { method: 'DELETE' })
    store.removeTask(props.task.id)
  } catch (e) {
    // ignore
  }
}
</script>
