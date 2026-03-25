<template>
  <div v-if="unseenTasks.length > 0" class="border-b border-gray-800 pb-2 mb-1">
    <div class="flex items-center justify-between px-4 py-1.5">
      <span class="text-xs text-gray-500 uppercase tracking-wider font-semibold">近期更新</span>
      <span class="text-xs text-gray-600 bg-gray-800 rounded-full px-1.5 py-0.5 tabular-nums">{{ unseenTasks.length }}</span>
    </div>
    <div
      v-for="task in unseenTasks"
      :key="task.id"
      class="flex items-center gap-2 px-4 py-1.5 cursor-pointer rounded text-sm transition-colors hover:bg-gray-800/50 group"
      @click="handleClick(task)"
    >
      <span class="text-xs flex-shrink-0" :class="statusClass(task)">●</span>
      <span class="truncate flex-1 text-gray-300">{{ task.name || 'Untitled' }}</span>
      <span class="text-xs text-gray-600 flex-shrink-0 truncate max-w-[5rem]">{{ agentName(task.agent_id) }}</span>
      <button
        class="text-gray-600 hover:text-gray-300 text-xs flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
        title="关闭"
        @click.stop="store.dismissUnseenTask(task.id)"
      >×</button>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useAgentStore } from '../stores/agentStore'

const store = useAgentStore()

const unseenTasks = computed(() =>
  store.unseenTaskIds
    .map(id => store.tasks[id])
    .filter(Boolean)
)

function agentName(agentId) {
  return store.agents.find(a => a.id === agentId)?.name || ''
}

function statusClass(task) {
  switch (task.status) {
    case 'running': return 'text-yellow-400'
    case 'waiting': return 'text-blue-400'
    case 'success': return 'text-green-500'
    case 'failed': return 'text-red-500'
    default: return 'text-gray-600'
  }
}

function handleClick(task) {
  store.selectTask(task.id)
  // Notify AgentGroup to expand and reveal this task
  window.dispatchEvent(new CustomEvent('reveal-task', { detail: { taskId: task.id, agentId: task.agent_id } }))
}
</script>
