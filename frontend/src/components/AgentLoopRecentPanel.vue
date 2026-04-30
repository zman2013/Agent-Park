<template>
  <div v-if="loops.length > 0" class="border-b border-gray-800 pb-2 mb-1 max-h-[35vh] flex flex-col">
    <div class="flex items-center justify-between px-4 py-1.5 flex-shrink-0">
      <span class="text-xs text-gray-500 uppercase tracking-wider font-semibold">AgentLoop 近期更新</span>
      <span class="text-xs text-gray-600 bg-gray-800 rounded-full px-1.5 py-0.5 tabular-nums">{{ loops.length }}</span>
    </div>
    <div class="overflow-auto flex-1">
      <div
        v-for="loop in loops"
        :key="loop.loop_id"
        class="flex items-center gap-2 px-4 py-1.5 cursor-pointer rounded text-sm transition-colors group"
        :class="selected(loop) ? 'bg-gray-800/80' : 'hover:bg-gray-800/50'"
        @click="handleClick(loop)"
      >
        <span class="text-xs flex-shrink-0" :class="statusClass(loop)">●</span>
        <span class="truncate flex-1 text-gray-300" :title="loopTitle(loop)">{{ loopLabel(loop) }}</span>
        <span class="text-xs text-gray-600 flex-shrink-0 tabular-nums" :title="`累计成本 ¥${(loop.total_cost_cny || 0).toFixed(2)}`">c{{ loop.cycle || 0 }}</span>
        <button
          class="text-gray-600 hover:text-gray-300 text-xs flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
          title="从列表移除"
          @click.stop="store.dismissAgentLoopRecent(loop.loop_id)"
        >×</button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted } from 'vue'
import { useAgentStore } from '../stores/agentStore'
import { agentloopStatusColor } from '../utils/agentloopStatus'

const store = useAgentStore()

const loops = computed(() => {
  // newest first, filter to last 7 days, cap at 5.
  // Keep running loops visible even if dismissed — user wants the sidebar
  // entry to stick around until the loop finishes.
  const cutoffMs = Date.now() - 7 * 86400 * 1000
  return (store.agentloops || [])
    .filter(l => !l.dismissed || l.status === 'running')
    .filter(l => {
      const ts = Date.parse(l.started_at || '')
      return Number.isFinite(ts) ? ts >= cutoffMs : true
    })
    .slice(0, 5)
})

function selected(loop) {
  return store.selectedAgentLoopId === loop.loop_id
}

function statusClass(loop) {
  return agentloopStatusColor(loop.status)
}

function loopLabel(loop) {
  const base = loop.cwd_basename || 'loop'
  const ws = loop.workspace || '?'
  return `${base} / ${ws}`
}

function loopTitle(loop) {
  const ws = loop.workspace || '?'
  return `${loop.cwd}\nworkspace: ${ws}`
}

function handleClick(loop) {
  store.selectAgentLoop(loop.loop_id)
  // Only dismiss finished loops on click — running loops should stay visible
  // until they stop, so users don't lose track of in-progress work.
  if (loop.status !== 'running') {
    store.dismissAgentLoopRecent(loop.loop_id)
  }
}

let pollTimer = null
onMounted(() => {
  store.fetchAgentLoops()
  pollTimer = setInterval(() => store.fetchAgentLoops(), 3000)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>
