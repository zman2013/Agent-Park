<template>
  <Transition name="memory-slide">
    <div
      v-if="visible"
      class="flex flex-col border-t border-gray-700 bg-[#0d0d0d]"
      style="height: 50%"
    >
      <!-- Title bar -->
      <div class="flex items-center justify-between px-3 py-1.5 border-b border-gray-800 shrink-0 select-none bg-[#111]">
        <div class="flex items-center gap-2 text-xs text-gray-400 min-w-0">
          <span class="shrink-0">&#x1F9E0;</span>
          <span class="font-medium truncate">Memory: {{ agentName || agentId }}</span>
        </div>
        <div class="flex items-center gap-3 shrink-0 ml-2">
          <span class="text-xs text-gray-600">⌘K</span>
          <button
            class="text-gray-600 hover:text-gray-300 text-xs px-1 leading-none"
            @click="$emit('close')"
          >✕</button>
        </div>
      </div>

      <!-- Add memory input -->
      <div class="px-3 py-2 border-b border-gray-800 shrink-0">
        <div class="flex gap-2 items-end">
          <textarea
            v-model="newContent"
            rows="2"
            class="flex-1 bg-[#111] border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 outline-none focus:border-gray-500 resize-none font-mono leading-5"
            placeholder="添加新的 memory 条目..."
            :disabled="saving"
            @keydown.enter.ctrl.prevent="saveEntry"
            @keydown.enter.meta.prevent="saveEntry"
          ></textarea>
          <button
            class="text-xs bg-blue-700 hover:bg-blue-600 disabled:opacity-40 text-white px-3 py-1.5 rounded shrink-0"
            :disabled="saving || !newContent.trim()"
            @click="saveEntry"
          >{{ saving ? '压缩中...' : '保存' }}</button>
        </div>
        <div v-if="saveError" class="mt-1 text-xs text-red-400">{{ saveError }}</div>
        <div v-if="saveErrorCompressed" class="mt-1 text-xs text-gray-400 bg-gray-800/60 rounded px-2 py-1">
          <span class="text-gray-600">压缩结果：</span>{{ saveErrorCompressed }}
        </div>
      </div>

      <!-- Memory list -->
      <div ref="listEl" class="flex-1 overflow-auto">
        <!-- Loading state -->
        <div v-if="loading" class="p-4 text-xs text-gray-600 text-center">加载中...</div>

        <!-- Empty state -->
        <div v-else-if="entries.length === 0" class="p-4 text-xs text-gray-600 text-center">
          暂无 memory 条目
        </div>

        <!-- Entries (newest first) -->
        <div
          v-for="entry in entries"
          :key="entry.line_index"
          class="px-3 py-2 border-b border-gray-800/60 hover:bg-gray-800/20 group"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="flex-1 min-w-0">
              <div class="text-xs text-gray-600 mb-0.5 font-mono">
                {{ formatTs(entry.timestamp) }}
                <span class="text-gray-700 ml-1">{{ entry.type }}</span>
              </div>
              <div class="text-xs text-gray-300 leading-5 whitespace-pre-wrap break-words">{{ entry.content }}</div>
            </div>
            <button
              class="text-gray-700 hover:text-red-400 text-xs shrink-0 opacity-0 group-hover:opacity-100 transition-opacity px-1"
              title="删除"
              @click="deleteEntry(entry.line_index)"
            >✕</button>
          </div>
        </div>
      </div>
    </div>
  </Transition>
</template>

<script setup>
import { ref, watch, nextTick } from 'vue'
import { useAgentStore } from '../stores/agentStore'

const props = defineProps({
  visible: { type: Boolean, default: false },
  agentId: { type: String, default: null },
  agentName: { type: String, default: '' },
})

const emit = defineEmits(['close'])

const store = useAgentStore()

const entries = ref([])
const newContent = ref('')
const saving = ref(false)
const saveError = ref('')
const saveErrorCompressed = ref('')  // compressed preview when over limit
const loading = ref(false)
const listEl = ref(null)

watch(() => [props.visible, props.agentId], ([visible, agentId]) => {
  if (visible && agentId) {
    fetchMemory()
  }
}, { immediate: true })

async function fetchMemory() {
  if (!props.agentId) return
  loading.value = true
  try {
    const res = await fetch(`/api/agents/${props.agentId}/memory`)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    entries.value = await res.json()
    store.setAgentMemory(props.agentId, entries.value)
  } catch (e) {
    store.addToast(`Failed to load memory: ${e.message}`, 'error')
  } finally {
    loading.value = false
  }
}

async function saveEntry() {
  const content = newContent.value.trim()
  if (!content || saving.value) return
  saving.value = true
  saveError.value = ''
  saveErrorCompressed.value = ''
  try {
    const res = await fetch(`/api/agents/${props.agentId}/memory`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      saveError.value = err.detail || `HTTP ${res.status}`
      if (err.compressed) saveErrorCompressed.value = err.compressed
      return
    }
    newContent.value = ''
    // Refresh list to get correct line_index values
    await fetchMemory()
    nextTick(() => {
      if (listEl.value) listEl.value.scrollTop = 0
    })
  } catch (e) {
    saveError.value = e.message
  } finally {
    saving.value = false
  }
}

async function deleteEntry(lineIndex) {
  try {
    const res = await fetch(`/api/agents/${props.agentId}/memory/${lineIndex}`, {
      method: 'DELETE',
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    // Refresh to get updated line_index values
    await fetchMemory()
  } catch (e) {
    store.addToast(`Failed to delete memory: ${e.message}`, 'error')
  }
}

function formatTs(ts) {
  if (!ts) return ''
  try {
    return new Date(ts).toLocaleString('zh-CN', {
      month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return ts
  }
}
</script>

<style scoped>
.memory-slide-enter-active,
.memory-slide-leave-active {
  transition: height 0.2s ease, opacity 0.2s ease;
  overflow: hidden;
}
.memory-slide-enter-from,
.memory-slide-leave-to {
  height: 0 !important;
  opacity: 0;
}
</style>
