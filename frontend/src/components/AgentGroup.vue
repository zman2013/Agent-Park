<template>
  <div class="mb-2">
    <!-- Agent Header -->
    <div
      class="flex items-center gap-2 px-2 py-1.5 cursor-pointer hover:bg-gray-800/50 rounded text-sm text-gray-300"
      @click="store.toggleAgent(agent.id)"
    >
      <span class="text-xs text-gray-500 w-4">{{ isOpen ? '▼' : '▶' }}</span>
      <span class="font-medium">{{ agent.name }}</span>
      <span class="text-xs text-gray-600 ml-auto">{{ taskCount }}</span>
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
</script>
