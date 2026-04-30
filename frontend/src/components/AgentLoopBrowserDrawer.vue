<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-40 bg-black/40"
      @click.self="$emit('close')"
    >
      <aside
        class="absolute top-0 right-0 h-full w-[480px] max-w-full bg-[#111] border-l border-gray-800 shadow-2xl flex flex-col"
        @click.stop
      >
        <div class="flex items-center gap-3 px-4 py-3 border-b border-gray-800 flex-shrink-0">
          <span class="text-sm text-gray-300 font-semibold">Workspace 浏览器</span>
          <span class="text-xs text-gray-600 truncate flex-1" :title="cwdFilter || '全部项目'">
            {{ cwdFilter ? cwdFilter : '全部项目' }}
          </span>
          <button
            class="text-gray-500 hover:text-gray-200 text-lg leading-none px-1"
            title="关闭 (Esc)"
            @click="$emit('close')"
          >×</button>
        </div>

        <div class="px-4 pt-3 pb-2 flex-shrink-0">
          <input
            ref="searchInput"
            v-model="query"
            type="text"
            placeholder="搜索 cwd / workspace / design 路径"
            class="w-full px-2 py-1.5 bg-[#0d0d0d] border border-gray-800 focus:border-gray-700 rounded text-xs text-gray-200 placeholder-gray-600 outline-none"
          />
        </div>

        <div class="px-4 pb-2 flex items-center gap-1 text-xs flex-shrink-0 flex-wrap">
          <button
            v-for="tab in statusTabs"
            :key="tab.key"
            class="px-2 py-0.5 rounded transition-colors"
            :class="statusFilter === tab.key
              ? 'bg-gray-700 text-gray-100'
              : 'bg-gray-800/50 text-gray-500 hover:text-gray-300 hover:bg-gray-800'"
            @click="statusFilter = tab.key"
          >{{ tab.label }} {{ tab.count }}</button>
        </div>

        <div class="overflow-auto flex-1 px-2 pb-4">
          <div v-if="!grouped.length" class="px-4 py-8 text-center text-xs text-gray-600">
            没有匹配的 workspace
          </div>
          <template v-for="group in grouped" :key="group.cwd">
            <div
              class="sticky top-0 bg-[#111] border-b border-gray-800/60 px-3 py-1.5 cursor-pointer hover:bg-gray-800/40 transition-colors"
              @click="toggleGroup(group.cwd)"
            >
              <div class="flex items-center gap-2">
                <span class="text-gray-500 text-xs">{{ collapsed[group.cwd] ? '▸' : '▾' }}</span>
                <span class="text-gray-300 text-xs font-semibold truncate">{{ group.basename }}</span>
                <span class="text-gray-700 text-xs">·</span>
                <span class="text-gray-600 text-xs tabular-nums">{{ group.items.length }} workspace{{ group.items.length > 1 ? 's' : '' }}</span>
              </div>
              <div class="text-gray-600 text-[10px] truncate ml-5 mt-0.5" :title="group.cwd">{{ group.cwd }}</div>
            </div>
            <div v-if="!collapsed[group.cwd]">
              <div
                v-for="loop in group.items"
                :key="loop.loop_id"
                class="group px-3 py-2 cursor-pointer text-xs border-b border-gray-800/40 transition-colors"
                :class="selected(loop) ? 'bg-gray-800/70' : 'hover:bg-gray-800/40'"
                @click="handleClick(loop)"
              >
                <div class="flex items-center gap-2">
                  <span class="flex-shrink-0 text-xs" :class="statusColor(loop)">●</span>
                  <span class="truncate text-gray-200 flex-1 font-mono" :title="loop.workspace">{{ loop.workspace || '—' }}</span>
                  <span class="tabular-nums text-gray-500 flex-shrink-0" :title="`累计成本 ¥${(loop.total_cost_cny || 0).toFixed(2)}`">c{{ loop.cycle || 0 }}</span>
                  <span class="flex-shrink-0" :class="statusColor(loop)">{{ loop.status }}</span>
                  <button
                    v-if="loop.status !== 'running'"
                    class="flex-shrink-0 text-gray-600 hover:text-green-400 transition-colors opacity-0 group-hover:opacity-100 px-1"
                    title="复用此 workspace 重新启动"
                    @click.stop="handleStart(loop)"
                  >▶</button>
                </div>
                <div class="mt-1 ml-5 text-gray-600 truncate" :title="loop.design_path">
                  {{ loop.design_path || '' }}
                </div>
                <div class="mt-0.5 ml-5 text-gray-700 tabular-nums">
                  {{ formatTimeRange(loop) }}
                </div>
              </div>
            </div>
          </template>
        </div>
      </aside>
    </div>
  </Teleport>
</template>

<script setup>
import { computed, ref, watch, nextTick } from 'vue'
import { useAgentStore } from '../stores/agentStore'
import { agentloopStatusColor } from '../utils/agentloopStatus'

const props = defineProps({
  open: { type: Boolean, default: false },
  // When set, filter to a single cwd (opened from AgentLoopPanel for "show
  // other workspaces of this project"). When null, show all cwds.
  cwdFilter: { type: String, default: null },
})
const emit = defineEmits(['close'])

const store = useAgentStore()

const query = ref('')
const statusFilter = ref('all')
const collapsed = ref({})
const searchInput = ref(null)

const cwdScoped = computed(() =>
  props.cwdFilter
    ? store.findAgentLoopsByCwd(props.cwdFilter)
    : (store.agentloops || [])
)

const pool = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return cwdScoped.value
  return cwdScoped.value.filter(l =>
    (l.cwd || '').toLowerCase().includes(q) ||
    (l.workspace || '').toLowerCase().includes(q) ||
    (l.design_path || '').toLowerCase().includes(q)
  )
})

const statusTabs = computed(() => {
  const counts = { all: pool.value.length, running: 0, done: 0, exhausted: 0, stopped: 0 }
  for (const l of pool.value) {
    if (counts[l.status] !== undefined) counts[l.status]++
  }
  return [
    { key: 'all', label: '全部', count: counts.all },
    { key: 'running', label: '运行中', count: counts.running },
    { key: 'done', label: '已完成', count: counts.done },
    { key: 'stopped', label: '已停止', count: counts.stopped },
    { key: 'exhausted', label: '异常', count: counts.exhausted },
  ]
})

const filtered = computed(() =>
  statusFilter.value === 'all'
    ? pool.value
    : pool.value.filter(l => l.status === statusFilter.value)
)

const grouped = computed(() => {
  const byCwd = new Map()
  for (const loop of filtered.value) {
    const key = loop.cwd || ''
    if (!byCwd.has(key)) {
      byCwd.set(key, {
        cwd: key,
        basename: loop.cwd_basename || key.split('/').pop() || '—',
        items: [],
      })
    }
    byCwd.get(key).items.push(loop)
  }
  const groups = [...byCwd.values()]
  for (const g of groups) {
    g.items.sort((a, b) => (b.started_at || '').localeCompare(a.started_at || ''))
  }
  // Freshest project first so newly-started loops surface at the top.
  groups.sort((a, b) =>
    (b.items[0]?.started_at || '').localeCompare(a.items[0]?.started_at || '')
  )
  return groups
})

function selected(loop) {
  return store.selectedAgentLoopId === loop.loop_id
}

function statusColor(loop) {
  return agentloopStatusColor(loop.status)
}

function toggleGroup(cwd) {
  collapsed.value[cwd] = !collapsed.value[cwd]
}

function handleClick(loop) {
  store.selectAgentLoop(loop.loop_id)
  emit('close')
}

async function handleStart(loop) {
  await store.startAgentLoop(loop.loop_id)
}

function formatTimeRange(loop) {
  const startMs = Date.parse(loop.started_at || '')
  if (!Number.isFinite(startMs)) return ''
  const start = fmtCst(startMs)
  if (loop.status === 'running') return `${start} → 运行中`
  const endMs = Date.parse(loop.stopped_at || '')
  if (!Number.isFinite(endMs)) return start
  const end = fmtCst(endMs)
  const dur = Math.max(0, Math.round((endMs - startMs) / 60000))
  return `${start} → ${end} (${dur}min)`
}

function fmtCst(ms) {
  const d = new Date(ms + 8 * 3600 * 1000)
  const mm = String(d.getUTCMonth() + 1).padStart(2, '0')
  const dd = String(d.getUTCDate()).padStart(2, '0')
  const HH = String(d.getUTCHours()).padStart(2, '0')
  const MM = String(d.getUTCMinutes()).padStart(2, '0')
  return `${mm}-${dd} ${HH}:${MM}`
}

function onKeydown(e) {
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('close')
  }
}

watch(() => props.open, async (v) => {
  if (v) {
    query.value = ''
    statusFilter.value = 'all'
    collapsed.value = {}
    window.addEventListener('keydown', onKeydown)
    await nextTick()
    searchInput.value?.focus()
  } else {
    window.removeEventListener('keydown', onKeydown)
  }
})
</script>
