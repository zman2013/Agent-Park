<template>
  <div class="mb-2">
    <div
      v-if="showNewTaskModal"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 px-4"
      @click="closeNewTaskModal"
    >
      <div
        class="w-full max-w-md rounded-xl border border-gray-700 bg-[#161616] p-4 shadow-2xl"
        @click.stop
      >
        <div class="mb-1 text-sm font-medium text-gray-200">Create Task</div>
        <div class="mb-3 text-xs text-gray-500">{{ agent.name }}</div>
        <input
          ref="newTaskInput"
          v-model="newTaskName"
          class="w-full bg-[#111] border border-gray-700 rounded px-3 py-2 text-sm outline-none focus:border-gray-500"
          placeholder="Enter task name"
          @keydown.esc.prevent="closeNewTaskModal"
          @keydown.enter.prevent="submitNewTask"
        />
        <div class="mt-3 flex justify-end gap-2">
          <button
            class="text-xs text-gray-500 hover:text-gray-300 px-2 py-1"
            @click="closeNewTaskModal"
          >Cancel</button>
          <button
            class="text-xs bg-green-700 hover:bg-green-600 text-white px-3 py-1 rounded disabled:opacity-40 disabled:cursor-not-allowed"
            :disabled="!newTaskName.trim()"
            @click="submitNewTask"
          >Create</button>
        </div>
      </div>
    </div>

    <!-- Agent Header -->
    <div
      class="relative flex items-center px-2 py-1.5 cursor-pointer hover:bg-gray-800/50 rounded text-sm text-gray-300 group"
      @click="store.toggleAgent(agent.id)"
    >
      <!-- 展开/折叠箭头 -->
      <span class="text-xs text-gray-500 w-4 shrink-0">{{ isOpen ? '▼' : '▶' }}</span>

      <!-- Title + Command，充分利用横向空间 -->
      <div class="flex items-center gap-2 flex-1 min-w-0 overflow-hidden">
        <span class="font-medium shrink-0">{{ agent.name }}</span>
        <span class="text-xs text-gray-500 font-mono truncate">{{ agent.command }}</span>
      </div>

      <!-- Hover 时显示的右侧按钮层（绝对定位，覆盖在右侧） -->
      <div
        class="absolute right-0 top-0 bottom-0 flex items-center gap-1 pr-2 pl-8
               opacity-0 group-hover:opacity-100 transition-opacity
               bg-gradient-to-r from-transparent via-[#111]/95 to-[#111]"
      >
        <!-- 配置按钮 -->
        <button
          class="text-gray-600 hover:text-gray-300 transition-colors"
          title="Edit agent"
          @click.stop="showEdit = !showEdit"
        >⚙</button>
        <!-- 文件浏览器 -->
        <button
          class="transition-colors px-0.5"
          :class="agent.cwd ? 'text-gray-600 hover:text-gray-300' : 'text-gray-800 cursor-not-allowed'"
          :disabled="!agent.cwd"
          title="Browse files"
          @click.stop="openFiles()"
        >📁</button>
        <!-- Task 数量 -->
        <span class="text-xs text-gray-600 px-0.5">{{ taskCount }}</span>
        <!-- Pin 按钮 -->
        <button
          class="transition-colors px-0.5"
          :class="agent.pinned ? 'text-gray-400' : 'text-gray-600 hover:text-gray-400'"
          :title="agent.pinned ? 'Unpin agent' : 'Pin to top'"
          @click.stop="agent.pinned ? store.unpinAgent(agent.id) : store.pinAgent(agent.id)"
        >{{ agent.pinned ? '★' : '☆' }}</button>
        <!-- 上下移动按钮 -->
        <span class="flex gap-0.5">
          <button
            class="text-gray-600 hover:text-gray-300 transition-colors px-0.5"
            title="Move up"
            @click.stop="store.moveAgentUp(agent.id)"
          >↑</button>
          <button
            class="text-gray-600 hover:text-gray-300 transition-colors px-0.5"
            title="Move down"
            @click.stop="store.moveAgentDown(agent.id)"
          >↓</button>
        </span>
      </div>
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
      <div>
        <label class="text-xs text-gray-500 block mb-1">Command</label>
        <input
          v-model="editCommand"
          class="w-full bg-[#111] border border-gray-700 rounded px-2 py-1 text-sm outline-none focus:border-gray-500"
          placeholder="cco"
        />
      </div>
      <div>
        <label class="text-xs text-gray-500 block mb-1">Shared Memory</label>
        <select
          v-model="editSharedMemoryAgentId"
          class="w-full bg-[#111] border border-gray-700 rounded px-2 py-1 text-sm outline-none focus:border-gray-500"
        >
          <option value="">— own memory —</option>
          <option
            v-for="a in otherAgents"
            :key="a.id"
            :value="a.id"
          >{{ a.name }}</option>
        </select>
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


    <!-- Tasks -->
    <div v-if="isOpen" class="ml-2">
      <!-- New Task Button (top) -->
      <div
        class="flex items-center gap-1 px-4 py-1 text-xs text-gray-600 cursor-pointer hover:text-gray-400 transition-colors"
        @click="handleNewTask"
      >
        <span>+</span>
        <span>new task</span>
      </div>

      <!-- Visible tasks (reversed, limited to 5 unless expanded) -->
      <template v-for="taskId in visibleTaskIds" :key="taskId">
        <TaskItem
          v-if="store.tasks[taskId]"
          :task="store.tasks[taskId]"
          :data-task-id="taskId"
        />
      </template>

      <!-- Show more / less toggle -->
      <div
        v-if="taskCount > TASK_LIMIT"
        class="flex items-center gap-1 px-4 py-1 text-xs text-gray-600 cursor-pointer hover:text-gray-400 transition-colors"
        @click="showAllTasks = !showAllTasks"
      >
        <span v-if="!showAllTasks">▸ show {{ taskCount - TASK_LIMIT }} more</span>
        <span v-else>▴ show less</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useAgentStore } from '../stores/agentStore'
import TaskItem from './TaskItem.vue'

const TASK_LIMIT = 5

const props = defineProps({
  agent: { type: Object, required: true },
})

const store = useAgentStore()
const isOpen = computed(() => !store.isCollapsed(props.agent.id))
const taskCount = computed(() => props.agent.task_ids?.length || 0)

const showEdit = ref(false)
const editName = ref('')
const editCwd = ref('')
const editCommand = ref('')
const editSharedMemoryAgentId = ref('')
const showAllTasks = ref(false)
const showNewTaskModal = ref(false)
const newTaskName = ref('')
const newTaskInput = ref(null)

// Agents other than the current one (for shared memory picker)
const otherAgents = computed(() => store.agents.filter(a => a.id !== props.agent.id))

// Display name of the agent whose memory is being shared
const sharedMemoryAgentName = computed(() => {
  const sid = props.agent.shared_memory_agent_id
  if (!sid) return null
  const a = store.agents.find(a => a.id === sid)
  return a ? a.name : null
})

// Reversed task ids: sort by updated_at desc, fallback to creation order reversed
const reversedTaskIds = computed(() => {
  const ids = [...(props.agent.task_ids || [])]
  const orderMap = new Map(ids.map((id, index) => [id, index]))
  return ids.sort((a, b) => {
    const ta = store.tasks[a]?.updated_at || ''
    const tb = store.tasks[b]?.updated_at || ''
    if (tb > ta) return 1
    if (tb < ta) return -1
    // same timestamp: preserve original order reversed
    return (orderMap.get(b) || 0) - (orderMap.get(a) || 0)
  })
})

// Tasks to display: limited to TASK_LIMIT unless expanded
const visibleTaskIds = computed(() => {
  if (showAllTasks.value || taskCount.value <= TASK_LIMIT) {
    return reversedTaskIds.value
  }
  return reversedTaskIds.value.slice(0, TASK_LIMIT)
})

// Sync edit fields when panel opens
watch(showEdit, (v) => {
  if (v) {
    editName.value = props.agent.name || ''
    editCwd.value = props.agent.cwd || ''
    editCommand.value = props.agent.command || 'cco'
    editSharedMemoryAgentId.value = props.agent.shared_memory_agent_id || ''
  }
})

watch(showNewTaskModal, async (visible) => {
  if (!visible) {
    newTaskName.value = ''
    return
  }
  await nextTick()
  newTaskInput.value?.focus()
  newTaskInput.value?.select()
})

async function saveEdit() {
  const body = {}
  if (editName.value !== props.agent.name) body.name = editName.value
  if (editCwd.value !== props.agent.cwd) body.cwd = editCwd.value
  if (editCommand.value !== props.agent.command) body.command = editCommand.value

  const currentShared = props.agent.shared_memory_agent_id || ''
  if (editSharedMemoryAgentId.value !== currentShared) {
    if (editSharedMemoryAgentId.value) {
      body.shared_memory_agent_id = editSharedMemoryAgentId.value
    } else {
      body.clear_shared_memory = true
    }
  }

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
  showNewTaskModal.value = true
}

function closeNewTaskModal() {
  showNewTaskModal.value = false
}

function submitNewTask() {
  const name = newTaskName.value.trim()
  if (!name) return
  window.dispatchEvent(new CustomEvent('create-task', {
    detail: { agentId: props.agent.id, name }
  }))
  closeNewTaskModal()
}

function openMemory() {
  window.dispatchEvent(new CustomEvent('open-memory', {
    detail: { agentId: props.agent.id }
  }))
}

function openFiles() {
  window.dispatchEvent(new CustomEvent('open-files', {
    detail: { agentId: props.agent.id }
  }))
}

function onRevealTask(e) {
  const { taskId, agentId } = e.detail
  if (agentId !== props.agent.id) return
  // Expand the agent if collapsed
  if (store.isCollapsed(props.agent.id)) {
    store.toggleAgent(props.agent.id)
  }
  // If task is not in visibleTaskIds, expand all tasks
  if (!visibleTaskIds.value.includes(taskId)) {
    showAllTasks.value = true
  }
  // Scroll the task item into view after DOM update
  nextTick(() => {
    const el = document.querySelector(`[data-task-id="${taskId}"]`)
    el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  })
}

onMounted(() => {
  window.addEventListener('reveal-task', onRevealTask)
})

onUnmounted(() => {
  window.removeEventListener('reveal-task', onRevealTask)
})
</script>
