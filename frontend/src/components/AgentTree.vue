<template>
  <div class="bg-[#111] border-r border-gray-800 flex flex-col h-full overflow-hidden">
    <div class="p-4 flex items-center justify-between">
      <span class="text-xs text-gray-500 uppercase tracking-wider font-semibold">Agents</span>
      <span
        v-if="usage.amount !== null"
        class="text-xs text-gray-500 tabular-nums"
        :title="'本月使用总金额（点击刷新）'"
        style="cursor: pointer"
        @click="fetchUsage"
      >¥{{ usage.amount }}</span>
      <span v-else-if="usage.loading" class="text-xs text-gray-600">...</span>
    </div>
    <div class="flex-1 overflow-auto px-2 pb-4">
      <AgentGroup
        v-for="agent in store.agents"
        :key="agent.id"
        :agent="agent"
      />

      <!-- New Agent Form -->
      <div v-if="showForm" class="mx-2 mb-2 p-3 bg-gray-800/60 rounded-lg border border-gray-700 space-y-2">
        <div>
          <label class="text-xs text-gray-500 block mb-1">Name</label>
          <input
            ref="nameInput"
            v-model="newName"
            class="w-full bg-[#111] border border-gray-700 rounded px-2 py-1 text-sm outline-none focus:border-gray-500"
            placeholder="my-agent"
            @keydown.enter="submitNewAgent"
          />
        </div>
        <div>
          <label class="text-xs text-gray-500 block mb-1">Working Directory</label>
          <input
            v-model="newCwd"
            class="w-full bg-[#111] border border-gray-700 rounded px-2 py-1 text-sm outline-none focus:border-gray-500"
            placeholder="/path/to/project"
            @keydown.enter="submitNewAgent"
          />
        </div>
        <div class="flex gap-2 justify-end">
          <button
            class="text-xs text-gray-500 hover:text-gray-300 px-2 py-1"
            @click="showForm = false"
          >Cancel</button>
          <button
            class="text-xs bg-green-700 hover:bg-green-600 text-white px-3 py-1 rounded"
            @click="submitNewAgent"
          >Create</button>
        </div>
      </div>

      <div
        v-else
        class="flex items-center gap-1 px-4 py-1.5 text-xs text-gray-600 cursor-pointer hover:text-gray-400 transition-colors"
        @click="openForm"
      >
        <span>+</span>
        <span>new agent</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick, onMounted } from 'vue'
import { useAgentStore } from '../stores/agentStore'
import AgentGroup from './AgentGroup.vue'

const store = useAgentStore()

const showForm = ref(false)
const newName = ref('')
const newCwd = ref('')
const nameInput = ref(null)

const usage = ref({ amount: null, loading: false })

async function fetchUsage() {
  usage.value.loading = true
  try {
    const res = await fetch('/api/ept-usage')
    const data = await res.json()
    usage.value.amount = data.amount !== null ? Number(data.amount).toFixed(2) : null
  } catch {
    usage.value.amount = null
  } finally {
    usage.value.loading = false
  }
}

onMounted(fetchUsage)

function openForm() {
  newName.value = ''
  newCwd.value = ''
  showForm.value = true
  nextTick(() => nameInput.value?.focus())
}

async function submitNewAgent() {
  const name = newName.value.trim()
  if (!name) return
  try {
    const res = await fetch('/api/agents', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, cwd: newCwd.value.trim() }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${res.status}`)
    }
    showForm.value = false
  } catch (e) {
    store.addToast(`Failed to create agent: ${e.message}`, 'error')
  }
}
</script>
