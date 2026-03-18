<template>
  <div class="mb-2">
    <!-- Agent Header -->
    <div
      class="flex items-center gap-2 px-2 py-1.5 cursor-pointer hover:bg-gray-800/50 rounded text-sm text-gray-300"
      @click="store.toggleAgent(agent.id)"
    >
      <span class="text-xs text-gray-500 w-4">{{ isOpen ? '▼' : '▶' }}</span>
      <span class="font-medium">{{ agent.name }}</span>
      <button
        class="text-gray-600 hover:text-gray-300 transition-colors ml-1"
        title="Set working directory"
        @click.stop="handleSetCwd"
      >⚙</button>
      <span class="text-xs text-gray-600 ml-auto">{{ taskCount }}</span>
    </div>

    <!-- Current cwd display -->
    <div v-if="agent.cwd" class="px-2 pl-8 text-xs text-gray-500 truncate" :title="agent.cwd">
      📂 {{ agent.cwd }}
    </div>

    <!-- Tasks -->
    <div v-if="isOpen" class="ml-2">
      <template v-for="taskId in agent.task_ids" :key="taskId">
        <TaskItem
          v-if="store.tasks[taskId]"
          :task="store.tasks[taskId]"
        />
      </template>

      <!-- New Task Button -->
      <div
        class="flex items-center gap-1 px-4 py-1 text-xs text-gray-600 cursor-pointer hover:text-gray-400 transition-colors"
        @click="handleNewTask"
      >
        <span>+</span>
        <span>new task</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useAgentStore } from '../stores/agentStore'
import TaskItem from './TaskItem.vue'

const props = defineProps({
  agent: { type: Object, required: true },
})

const store = useAgentStore()
const isOpen = computed(() => !store.isCollapsed(props.agent.id))
const taskCount = computed(() => props.agent.task_ids?.length || 0)

function handleNewTask() {
  const prompt = window.prompt('Enter task prompt:')
  if (!prompt) return

  window.dispatchEvent(new CustomEvent('create-task', {
    detail: { agentId: props.agent.id, prompt }
  }))
}

async function handleSetCwd() {
  const cwd = window.prompt('Enter working directory (cwd):', props.agent.cwd || '')
  if (cwd === null) return

  try {
    const res = await fetch(`/api/agents/${props.agent.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cwd }),
    })
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`)
    }
  } catch (e) {
    store.addToast(`Failed to update cwd: ${e.message}`, 'error')
  }
}
</script>
