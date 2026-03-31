<template>
  <Transition name="prompts-slide">
    <div
      v-if="visible"
      class="flex flex-col border-t border-gray-700 bg-[#0d0d0d]"
      style="height: 50%"
    >
      <!-- Title bar -->
      <div class="flex items-center justify-between px-3 py-1.5 border-b border-gray-800 shrink-0 select-none bg-[#111]">
        <div class="flex items-center gap-2 text-xs text-gray-400 min-w-0">
          <span class="shrink-0">📝</span>
          <span class="font-medium">Prompts</span>
        </div>
        <div class="flex items-center gap-3 shrink-0 ml-2">
          <span class="text-xs text-gray-600">⌘U</span>
          <button
            class="text-gray-600 hover:text-gray-300 text-xs px-1 leading-none"
            @click="$emit('close')"
          >✕</button>
        </div>
      </div>

      <!-- Add prompt input -->
      <div class="px-3 py-2 border-b border-gray-800 shrink-0">
        <div class="flex flex-col gap-1.5">
          <input
            v-model="newTitle"
            type="text"
            class="w-full bg-[#111] border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 outline-none focus:border-gray-500 font-mono"
            placeholder="标题（可选）"
            :disabled="saving"
          />
          <div class="flex gap-2 items-end">
            <textarea
              v-model="newContent"
              rows="2"
              class="flex-1 bg-[#111] border border-gray-700 rounded px-2 py-1 text-xs text-gray-200 outline-none focus:border-gray-500 resize-none font-mono leading-5"
              placeholder="添加新的 prompt 内容..."
              :disabled="saving"
              @keydown.enter.ctrl.prevent="saveEntry"
              @keydown.enter.meta.prevent="saveEntry"
            ></textarea>
            <button
              class="text-xs bg-blue-700 hover:bg-blue-600 disabled:opacity-40 text-white px-3 py-1.5 rounded shrink-0"
              :disabled="saving || !newContent.trim()"
              @click="saveEntry"
            >{{ saving ? '保存中...' : '保存' }}</button>
          </div>
        </div>
        <div v-if="saveError" class="mt-1 text-xs text-red-400">{{ saveError }}</div>
      </div>

      <!-- Prompts list -->
      <div ref="listEl" class="flex-1 overflow-auto">
        <!-- Loading state -->
        <div v-if="loading" class="p-4 text-xs text-gray-600 text-center">加载中...</div>

        <!-- Empty state -->
        <div v-else-if="entries.length === 0" class="p-4 text-xs text-gray-600 text-center">
          暂无 prompt 条目
        </div>

        <!-- Entries (newest first) -->
        <div
          v-for="entry in entries"
          :key="entry.id"
          class="px-3 py-2 border-b border-gray-800/60 hover:bg-gray-800/20 group cursor-pointer"
          @click="fillPrompt(entry)"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="flex-1 min-w-0">
              <div class="text-xs text-gray-600 mb-0.5 font-mono">
                {{ formatTs(entry.created_at) }}
              </div>
              <div v-if="entry.title" class="text-xs text-gray-300 font-medium mb-0.5 truncate">
                {{ entry.title }}
              </div>
              <div class="text-xs text-gray-500 leading-5 line-clamp-3 whitespace-pre-wrap break-words">{{ entry.content }}</div>
            </div>
            <button
              class="text-gray-700 hover:text-red-400 text-xs shrink-0 opacity-0 group-hover:opacity-100 transition-opacity px-1"
              title="删除"
              @click.stop="deleteEntry(entry.id)"
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
})

const emit = defineEmits(['close'])

const store = useAgentStore()

const entries = ref([])
const newTitle = ref('')
const newContent = ref('')
const saving = ref(false)
const saveError = ref('')
const loading = ref(false)
const listEl = ref(null)

watch(() => props.visible, (visible) => {
  if (visible) {
    fetchPrompts()
  }
}, { immediate: true })

async function fetchPrompts() {
  loading.value = true
  try {
    const res = await fetch('/api/prompts')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    entries.value = await res.json()
  } catch (e) {
    store.addToast(`Failed to load prompts: ${e.message}`, 'error')
  } finally {
    loading.value = false
  }
}

async function saveEntry() {
  const content = newContent.value.trim()
  if (!content || saving.value) return
  saving.value = true
  saveError.value = ''
  try {
    const res = await fetch('/api/prompts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: newTitle.value.trim(), content }),
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      saveError.value = err.detail || `HTTP ${res.status}`
      return
    }
    newTitle.value = ''
    newContent.value = ''
    await fetchPrompts()
    nextTick(() => {
      if (listEl.value) listEl.value.scrollTop = 0
    })
  } catch (e) {
    saveError.value = e.message
  } finally {
    saving.value = false
  }
}

async function deleteEntry(id) {
  try {
    const res = await fetch(`/api/prompts/${id}`, { method: 'DELETE' })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    await fetchPrompts()
  } catch (e) {
    store.addToast(`Failed to delete prompt: ${e.message}`, 'error')
  }
}

function fillPrompt(entry) {
  window.dispatchEvent(new CustomEvent('fill-prompt', {
    detail: { content: entry.content },
  }))
  emit('close')
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
.prompts-slide-enter-active,
.prompts-slide-leave-active {
  transition: height 0.2s ease, opacity 0.2s ease;
  overflow: hidden;
}
.prompts-slide-enter-from,
.prompts-slide-leave-to {
  height: 0 !important;
  opacity: 0;
}
</style>
