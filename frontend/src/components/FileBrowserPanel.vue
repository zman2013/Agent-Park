<template>
  <div class="flex flex-col flex-1 overflow-hidden min-h-0">
    <!-- Title bar -->
    <div class="flex items-center gap-2 px-3 py-2 border-b border-gray-800 text-xs text-gray-400 flex-shrink-0">
      <span class="text-gray-500">📁</span>
      <span class="font-mono truncate flex-1 min-w-0" :title="cwd">{{ cwd || '/' }}</span>
      <button
        class="text-gray-600 hover:text-gray-300 transition-colors ml-1 flex-shrink-0"
        title="Close file browser"
        @click="$emit('close')"
      >✕</button>
    </div>

    <!-- Loading / error state -->
    <div v-if="rootError" class="px-3 py-4 text-xs text-red-400">{{ rootError }}</div>

    <!-- Tree -->
    <div ref="treeEl" class="flex-1 overflow-auto py-1 min-h-0">
      <FileBrowserNode
        v-for="entry in rootEntries"
        :key="`${treeKey}-${entry.name}`"
        :entry="entry"
        :agent-id="agentId"
        :base-path="''"
        :depth="0"
        :expand-path="expandPathSegments"
        :highlight-path="initialPath"
        @file-select="$emit('file-select', $event)"
        @node-mounted="onNodeMounted"
      />
      <div v-if="rootLoading && rootEntries.length === 0" class="px-3 py-2 text-xs text-gray-600">Loading...</div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import FileBrowserNode from './FileBrowserNode.vue'

const props = defineProps({
  agentId: { type: String, required: true },
  initialPath: { type: String, default: '' },
})

defineEmits(['close', 'file-select'])

const cwd = ref('')
const rootEntries = ref([])
const rootLoading = ref(false)
const rootError = ref('')
const treeEl = ref(null)
// Increment to force re-mount of all FileBrowserNode when initialPath changes
const treeKey = ref(0)

// Split initialPath into segments for expand-path propagation
const expandPathSegments = computed(() => {
  if (!props.initialPath) return []
  return props.initialPath.split('/').filter(Boolean)
})

function onNodeMounted({ path, el }) {
  if (props.initialPath && path === props.initialPath && el) {
    el.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }
}

async function loadRoot() {
  rootLoading.value = true
  rootError.value = ''
  try {
    const res = await fetch(`/api/agents/${props.agentId}/files`)
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      throw new Error(data.detail || `HTTP ${res.status}`)
    }
    const data = await res.json()
    cwd.value = data.cwd
    rootEntries.value = data.entries
  } catch (e) {
    rootError.value = e.message
  } finally {
    rootLoading.value = false
  }
}

onMounted(loadRoot)

watch(() => props.agentId, loadRoot)

// When initialPath changes, force re-mount of the tree so nodes start fresh
// and auto-expand logic runs from scratch
watch(() => props.initialPath, () => {
  treeKey.value++
})
</script>
