<template>
  <div
    v-if="task"
    class="group flex items-center gap-2 px-4 py-1.5 cursor-pointer rounded text-sm transition-colors"
    :class="isActive ? 'bg-gray-800 text-gray-100' : 'hover:bg-gray-800/50 text-gray-400'"
    @click="store.selectTask(task.id)"
  >
    <span class="text-xs" :class="statusClass">{{ statusIcon }}</span>

    <!-- Editing mode -->
    <input
      v-if="editing"
      ref="editInput"
      v-model="editName"
      class="truncate flex-1 bg-transparent border border-gray-600 rounded px-1 outline-none text-sm"
      @keydown.enter="saveRename"
      @keydown.escape="cancelRename"
      @blur="saveRename"
      @click.stop
    />

    <!-- Display mode -->
    <span
      v-else
      class="truncate flex-1"
      @dblclick.stop="startRename"
    >{{ task.name || 'Untitled' }}</span>

    <button
      v-if="hasSession"
      class="text-gray-600 hover:text-blue-400 text-xs opacity-0 group-hover:opacity-100 transition-opacity"
      @click.stop="handleFork"
      title="Fork task"
    >
      ⑂
    </button>
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
import { computed, ref, nextTick } from 'vue'
import { useAgentStore } from '../stores/agentStore'

const props = defineProps({
  task: { type: Object, required: true },
})

const store = useAgentStore()
const isActive = computed(() => store.currentTaskId === props.task?.id)
const hasSession = computed(() => !!store.taskSessions[props.task?.id])

const editing = ref(false)
const editName = ref('')
const editInput = ref(null)

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

function startRename() {
  editName.value = props.task.name || ''
  editing.value = true
  nextTick(() => {
    editInput.value?.focus()
    editInput.value?.select()
  })
}

async function saveRename() {
  if (!editing.value) return
  editing.value = false
  const newName = editName.value.trim()
  if (!newName || newName === props.task.name) return

  try {
    const res = await fetch(`/api/tasks/${props.task.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: newName }),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
  } catch (e) {
    store.addToast(`Failed to rename task: ${e.message}`, 'error')
  }
}

function cancelRename() {
  editing.value = false
}

async function handleDelete() {
  try {
    await fetch(`/api/tasks/${props.task.id}`, { method: 'DELETE' })
    store.removeTask(props.task.id)
  } catch (e) {
    // ignore
  }
}

function handleFork() {
  window.dispatchEvent(new CustomEvent('fork-task', {
    detail: { taskId: props.task.id }
  }))
}
</script>
