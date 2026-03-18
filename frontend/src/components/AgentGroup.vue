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
        title="Edit agent"
        @click.stop="showEdit = !showEdit"
      >⚙</button>
      <span class="text-xs text-gray-600 ml-auto">{{ taskCount }}</span>
    </div>

    <!-- Edit Panel -->
    <div v-if="showEdit" class="mx-2 mb-2 p-3 bg-gray-800/60 rounded-lg border border-gray-700 space-y-2">
      <div>
        <label class="text-xs text-gray-500 block mb-1">Name</label>
        <input
          v-model="editName"
          class="w-full bg-[#111] border border-gray-700 rounded px-2 py-1 text-sm outline-none focus:border-gray-500"
        />
      </div>
      <div>
        <label class="text-xs text-gray-500 block mb-1">Working Directory</label>
        <input
          v-model="editCwd"
          class="w-full bg-[#111] border border-gray-700 rounded px-2 py-1 text-sm outline-none focus:border-gray-500"
          placeholder="/path/to/project"
        />
      </div>
      <div class="flex gap-2 justify-end">
        <button
          class="text-xs text-gray-500 hover:text-gray-300 px-2 py-1"
          @click="showEdit = false"
        >Cancel</button>
        <button
          class="text-xs bg-green-700 hover:bg-green-600 text-white px-3 py-1 rounded"
          @click="saveEdit"
        >Save</button>
      </div>
    </div>

    <!-- Current cwd display -->
    <div v-if="agent.cwd && !showEdit" class="px-2 pl-8 text-xs text-gray-500 truncate" :title="agent.cwd">
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
import { computed, ref, watch } from 'vue'
import { useAgentStore } from '../stores/agentStore'
import TaskItem from './TaskItem.vue'

const props = defineProps({
  agent: { type: Object, required: true },
})

const store = useAgentStore()
const isOpen = computed(() => !store.isCollapsed(props.agent.id))
const taskCount = computed(() => props.agent.task_ids?.length || 0)

const showEdit = ref(false)
const editName = ref('')
const editCwd = ref('')

// Sync edit fields when panel opens
watch(showEdit, (v) => {
  if (v) {
    editName.value = props.agent.name || ''
    editCwd.value = props.agent.cwd || ''
  }
})

async function saveEdit() {
  const body = {}
  if (editName.value !== props.agent.name) body.name = editName.value
  if (editCwd.value !== props.agent.cwd) body.cwd = editCwd.value

  if (Object.keys(body).length === 0) {
    showEdit.value = false
    return
  }

  try {
    const res = await fetch(`/api/agents/${props.agent.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    showEdit.value = false
  } catch (e) {
    store.addToast(`Failed to update agent: ${e.message}`, 'error')
  }
}

function handleNewTask() {
  const name = window.prompt('Enter task name:')
  if (!name) return

  window.dispatchEvent(new CustomEvent('create-task', {
    detail: { agentId: props.agent.id, name }
  }))
}
</script>
