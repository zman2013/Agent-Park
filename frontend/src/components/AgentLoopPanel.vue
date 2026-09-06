<template>
  <div class="flex flex-col flex-1 min-h-0">
    <!-- Top bar: cwd + cycle + cost + status + actions -->
    <div class="flex items-center gap-3 px-6 py-2 border-b border-gray-800 text-xs shrink-0 font-mono">
      <span class="text-gray-600">loop:</span>
      <span class="text-gray-400 truncate" :title="snap?.cwd || ''">{{ snap?.cwd || loopId }}</span>
      <button
        v-if="snap?.workspace"
        class="text-gray-600 hover:text-gray-300 transition-colors truncate flex items-center gap-1"
        :title="`workspace: ${snap.workspace} — 点击查看该项目下所有 workspace`"
        @click="browserOpen = true"
      >
        <span class="truncate">/ {{ snap.workspace }}</span>
        <span class="text-gray-500 text-[10px]">▾</span>
      </button>
      <span class="flex-1"></span>
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
        v-else-if="snap && snap.status"
        class="ml-2 px-2 py-0.5 bg-green-700/60 hover:bg-green-700 text-green-100 rounded text-xs transition-colors"
        :disabled="starting"
        :title="snap.exhausted_reason ? `从 ${snap.status} 状态继续执行 loop` : '启动 agentloop'"
        @click="handleStart"
      >{{ starting ? '启动中...' : '启动' }}</button>
      <button
        class="ml-1 text-gray-600 hover:text-gray-300 transition-colors px-1 text-xs"
        title="关闭"
        @click="handleClose"
      >×</button>
    </div>

    <!-- Corrupt gate file: neither approve nor reject can act on it. The loop
         fails closed on every start, so the only way out is a human fixing or
         deleting the file — say so instead of showing dead buttons. -->
    <div
      v-if="planReviewState === 'unreadable'"
      class="px-6 py-3 border-b shrink-0 text-xs bg-red-900/20 border-red-800/40"
    >
      <div class="font-semibold text-red-300">⛔ 计划闸门文件损坏</div>
      <div class="mt-1 text-gray-400">
        <span class="font-mono">plan-review.json</span> 无法解析，loop 拒绝执行未经批准的计划。
        请修复该文件后重新启动，或用 <span class="font-mono">--fresh</span> 重新规划。
        <span class="text-amber-300">不要只删除该文件</span>——todolist 还在，
        删掉闸门会让下次启动直接跑一份没人批准的计划。
      </div>
    </div>

    <!-- Plan review gate: the loop is paused until a human approves -->
    <div
      v-if="planReviewState === 'awaiting' || planReviewState === 'rejected'"
      class="px-6 py-3 border-b shrink-0 text-xs"
      :class="planReviewState === 'awaiting'
        ? 'bg-amber-900/20 border-amber-800/40'
        : 'bg-red-900/20 border-red-800/40'"
    >
      <div class="flex items-start gap-3">
        <div class="flex-1 min-w-0">
          <div class="font-semibold" :class="planReviewState === 'awaiting' ? 'text-amber-300' : 'text-red-300'">
            {{ planReviewState === 'awaiting' ? '⏸ 待确认计划' : '🚫 计划已驳回' }}
          </div>
          <div class="mt-1 text-gray-400">
            {{ planStats.items || 0 }} 项任务（dev {{ planStats.dev || 0 }} / qa {{ planStats.qa || 0 }}）
          </div>
          <!-- Coverage is the thing a human can actually judge; titles always
               look plausible. Keep it visually loud. -->
          <div v-if="planStats.unverified" class="mt-1 text-amber-300">
            ⚠ {{ planStats.unverified }} 个 dev item 无机器检查覆盖：
            <span class="font-mono">{{ (planStats.unverified_ids || []).join(', ') }}</span>
          </div>
          <div v-else-if="planStats.dev" class="mt-1 text-green-400">
            ✓ 全部 dev item 均有机器检查覆盖
          </div>
          <div v-if="planReviewNote" class="mt-1 text-gray-500">备注：{{ planReviewNote }}</div>
          <div v-if="planReviewState === 'rejected'" class="mt-1 text-gray-500">
            可编辑 todolist 后批准，或用 <span class="font-mono">--fresh</span> 重新规划
          </div>
        </div>
        <div class="flex flex-col gap-1 shrink-0">
          <button
            class="px-3 py-1 bg-green-700/70 hover:bg-green-700 text-green-50 rounded transition-colors disabled:opacity-50"
            :disabled="reviewing"
            @click="handleReview(true)"
          >{{ reviewing ? '处理中...' : '批准并执行' }}</button>
          <button
            v-if="planReviewState === 'awaiting'"
            class="px-3 py-1 bg-gray-700/70 hover:bg-gray-700 text-gray-200 rounded transition-colors disabled:opacity-50"
            :disabled="reviewing"
            @click="handleReview(false)"
          >驳回</button>
          <button
            class="px-3 py-1 text-gray-500 hover:text-gray-300 transition-colors"
            :title="editing ? '取消编辑' : '直接编辑 todolist.md，批准时绑定编辑后的版本'"
            @click="toggleEdit"
          >{{ editing ? '取消编辑' : '编辑计划' }}</button>
        </div>
      </div>
      <textarea
        v-if="editing"
        v-model="editedTodolist"
        spellcheck="false"
        class="mt-2 w-full h-64 bg-[#0d0d0d] border border-gray-700 rounded p-2 font-mono text-[11px] text-gray-300 focus:outline-none focus:border-gray-500"
      ></textarea>
      <div v-if="editing" class="mt-1 text-gray-600">
        批准时会保存并绑定以上内容；解析失败或没有 item 会被拒绝，原计划不受影响。
      </div>
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

    <AgentLoopBrowserDrawer
      :open="browserOpen"
      :cwd-filter="snap?.cwd || null"
      @close="browserOpen = false"
    />
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useAgentStore } from '../stores/agentStore'
import StreamJsonRenderer from './StreamJsonRenderer.vue'
import AgentLoopBrowserDrawer from './AgentLoopBrowserDrawer.vue'
import { agentloopStatusColor } from '../utils/agentloopStatus'

const props = defineProps({
  loopId: { type: String, required: true },
})

const emit = defineEmits(['close'])

const store = useAgentStore()

const selectedCycle = ref(null)
const stopping = ref(false)
const starting = ref(false)
const browserOpen = ref(false)
const reviewing = ref(false)
const editing = ref(false)
const editedTodolist = ref('')

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

const statusClass = computed(() => agentloopStatusColor(snap.value?.status))

const planReview = computed(() => snap.value?.plan_review || null)
// `consumed` / `approved` gates are not actionable — the banner only shows for
// states that need a human.
const planReviewState = computed(() => planReview.value?.state || null)
const planStats = computed(() => planReview.value?.stats || {})
const planReviewNote = computed(() => planReview.value?.note || '')

function toggleEdit() {
  if (editing.value) {
    editing.value = false
    return
  }
  editedTodolist.value = snap.value?.todolist?.raw || ''
  editing.value = true
}

async function handleReview(approve) {
  if (reviewing.value) return
  // Rejecting without a reason regenerates the same flawed plan, so require one.
  let note = null
  if (!approve) {
    note = window.prompt('驳回原因（会记录给下次 planner 参考）：')
    if (note === null) return
    if (!note.trim()) {
      note = null
    }
  }
  reviewing.value = true
  try {
    const ok = await store.reviewAgentLoopPlan(props.loopId, {
      approve,
      note,
      todolist: editing.value ? editedTodolist.value : null,
    })
    if (ok) editing.value = false
  } finally {
    reviewing.value = false
  }
}

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

async function handleStart() {
  if (starting.value) return
  starting.value = true
  try {
    await store.startAgentLoop(props.loopId)
  } finally {
    starting.value = false
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
