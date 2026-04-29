<template>
  <div class="flex flex-col flex-1 min-h-0">
    <!-- Top bar: cwd + cycle + cost + status + actions -->
    <div class="flex items-center gap-3 px-6 py-2 border-b border-gray-800 text-xs shrink-0 font-mono">
      <span class="text-gray-600">loop:</span>
      <span class="text-gray-400 truncate flex-1" :title="snap?.cwd || ''">{{ snap?.cwd || loopId }}</span>
      <template v-if="snap">
        <span class="text-gray-600">cycle</span>
        <span class="text-gray-300 tabular-nums">{{ snap.cycle || 0 }}</span>
        <span class="text-gray-700">|</span>
        <span class="text-gray-600">cost</span>
        <span class="text-gray-300">¥{{ (snap.total_cost_cny || 0).toFixed(2) }}</span>
        <span class="text-gray-700">|</span>
        <span :class="statusClass">{{ snap.status }}</span>
      </template>
      <button
        v-if="snap?.status === 'running'"
        class="ml-2 px-2 py-0.5 bg-red-700/60 hover:bg-red-700 text-red-100 rounded text-xs transition-colors"
        :disabled="stopping"
        @click="handleStop"
      >{{ stopping ? '停止中...' : '停止' }}</button>
      <button
        class="ml-1 text-gray-600 hover:text-gray-300 transition-colors px-1 text-xs"
        title="关闭"
        @click="handleClose"
      >×</button>
    </div>

    <!-- Exhausted reason -->
    <div v-if="snap?.exhausted_reason" class="px-6 py-2 text-xs text-orange-300 bg-orange-900/20 border-b border-orange-800/40 shrink-0">
      ⚠ {{ snap.exhausted_reason }}
    </div>

    <div class="flex-1 flex min-h-0 overflow-hidden">
      <!-- Left: todolist items -->
      <div class="w-72 shrink-0 border-r border-gray-800 overflow-auto">
        <div class="px-4 py-2 text-xs text-gray-500 uppercase tracking-wider font-semibold sticky top-0 bg-[#0d0d0d] border-b border-gray-800">
          Todolist ({{ todolistItems.length }})
        </div>
        <div v-if="!todolistItems.length" class="px-4 py-3 text-xs text-gray-600">
          尚未规划（planner 未运行或失败）
        </div>
        <div
          v-for="item in todolistItems"
          :key="item.id"
          class="px-4 py-2 text-xs border-b border-gray-800/60"
        >
          <div class="flex items-center gap-2">
            <span class="font-mono text-gray-500 shrink-0">{{ item.id }}</span>
            <span class="text-gray-700">·</span>
            <span class="text-gray-500 shrink-0">{{ item.type }}</span>
            <span class="ml-auto text-xs shrink-0" :class="itemStatusClass(item)">{{ item.status }}</span>
          </div>
          <div class="mt-1 text-gray-300 break-words">{{ item.title || '—' }}</div>
          <div v-if="item.attempt_log && item.attempt_log.length" class="mt-1 text-gray-600">
            attempts: {{ item.attempt_log.length }}
          </div>
        </div>
      </div>

      <!-- Center: cycle timeline -->
      <div class="w-48 shrink-0 border-r border-gray-800 overflow-auto">
        <div class="px-4 py-2 text-xs text-gray-500 uppercase tracking-wider font-semibold sticky top-0 bg-[#0d0d0d] border-b border-gray-800">
          Runs ({{ runs.length }})
        </div>
        <div v-if="!runs.length" class="px-4 py-3 text-xs text-gray-600">
          （尚无运行记录）
        </div>
        <div
          v-for="run in runsReversed"
          :key="run.filename"
          class="px-3 py-2 cursor-pointer text-xs border-b border-gray-800/60 transition-colors"
          :class="selectedCycle === run.cycle ? 'bg-gray-800/70' : 'hover:bg-gray-800/40'"
          @click="selectCycle(run.cycle)"
        >
          <div class="flex items-center gap-2">
            <span class="font-mono text-gray-500 tabular-nums">#{{ pad3(run.cycle) }}</span>
            <span class="text-gray-400">{{ run.actor }}</span>
          </div>
          <div v-if="run.item_id" class="text-gray-600 mt-0.5">{{ run.item_id }}</div>
        </div>
      </div>

      <!-- Right: run detail -->
      <div class="flex-1 overflow-auto p-4 min-w-0">
        <div v-if="selectedCycle === null" class="text-gray-600 text-sm text-center mt-20">
          左侧选择一轮查看日志
        </div>
        <template v-else>
          <div class="mb-3 text-xs text-gray-500 font-mono">
            cycle #{{ pad3(selectedCycle) }} · <span class="text-gray-400">{{ selectedRunActor }}</span>
            <span v-if="selectedRunItemId" class="text-gray-600"> · {{ selectedRunItemId }}</span>
          </div>
          <StreamJsonRenderer :key="selectedCycle" :lines="runLog.lines" />
        </template>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useAgentStore } from '../stores/agentStore'
import StreamJsonRenderer from './StreamJsonRenderer.vue'

const props = defineProps({
  loopId: { type: String, required: true },
})

const emit = defineEmits(['close'])

const store = useAgentStore()

const selectedCycle = ref(null)
const stopping = ref(false)

const snap = computed(() => store.agentloopSnapshot)
const runLog = computed(() => store.agentloopRunLog)

const todolistItems = computed(() => snap.value?.todolist?.items || [])
const runs = computed(() => snap.value?.runs || [])
const runsReversed = computed(() => [...runs.value].sort((a, b) => b.cycle - a.cycle))

const selectedRun = computed(() =>
  runs.value.find(r => r.cycle === selectedCycle.value) || null
)
const selectedRunActor = computed(() => selectedRun.value?.actor || '')
const selectedRunItemId = computed(() => selectedRun.value?.item_id || '')

const statusClass = computed(() => {
  switch (snap.value?.status) {
    case 'running': return 'text-yellow-400'
    case 'done': return 'text-green-400'
    case 'exhausted': return 'text-orange-400'
    case 'stopped': return 'text-gray-500'
    default: return 'text-gray-500'
  }
})

function itemStatusClass(item) {
  switch (item.status) {
    case 'done': return 'text-green-400'
    case 'ready_for_qa': return 'text-blue-400'
    case 'doing': return 'text-yellow-400'
    case 'pending': return 'text-gray-500'
    default: return 'text-gray-500'
  }
}

function pad3(n) {
  return String(n).padStart(3, '0')
}

function selectCycle(cycle) {
  selectedCycle.value = cycle
  store.fetchAgentLoopRunLog(props.loopId, cycle)
}

async function handleStop() {
  if (stopping.value) return
  stopping.value = true
  try {
    await store.stopAgentLoop(props.loopId)
  } finally {
    stopping.value = false
  }
}

function handleClose() {
  store.clearSelectedAgentLoop()
  emit('close')
}

// Auto-select the latest cycle when snapshot first loads / new runs appear.
watch(
  () => runs.value.length,
  (nv, ov) => {
    if (!runs.value.length) return
    if (selectedCycle.value === null) {
      selectCycle(runs.value[runs.value.length - 1].cycle)
      return
    }
    // New runs appeared while viewing the previously-latest cycle → follow to newest
    if (ov !== undefined && nv > ov) {
      const latest = runs.value[runs.value.length - 1].cycle
      if (selectedCycle.value === runs.value[ov - 1]?.cycle) {
        selectCycle(latest)
      }
    }
  },
  { immediate: true }
)

// Poll the snapshot every 3s while mounted.
let pollTimer = null
onMounted(() => {
  store.fetchAgentLoopSnapshot(props.loopId)
  pollTimer = setInterval(() => {
    store.fetchAgentLoopSnapshot(props.loopId)
    if (selectedCycle.value !== null) {
      store.fetchAgentLoopRunLog(props.loopId, selectedCycle.value)
    }
  }, 3000)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})

// Re-init when loopId prop changes
watch(() => props.loopId, (id) => {
  selectedCycle.value = null
  if (id) store.fetchAgentLoopSnapshot(id)
})
</script>
