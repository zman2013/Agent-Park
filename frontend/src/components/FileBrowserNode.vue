<template>
  <div>
    <!-- Entry row -->
    <div
      ref="rowEl"
      class="flex items-center gap-1 px-2 py-0.5 text-xs cursor-pointer hover:bg-gray-800/60 rounded transition-colors select-none"
      :class="{ 'bg-blue-900/40 text-blue-200': isHighlighted }"
      :style="{ paddingLeft: `${0.5 + depth * 1}rem` }"
      @click="onEntryClick"
    >
      <span class="text-gray-600 w-3 text-center flex-shrink-0">
        <template v-if="entry.type === 'dir'">{{ expanded ? '▼' : '▶' }}</template>
        <template v-else>·</template>
      </span>
      <span class="text-gray-500 flex-shrink-0">{{ entry.type === 'dir' ? '📁' : fileIcon(entry.name) }}<template v-if="entry.is_symlink">↗</template></span>
      <span class="truncate flex-1 min-w-0" :class="isHighlighted ? 'text-blue-200' : 'text-gray-300'">{{ entry.name }}<template v-if="entry.is_symlink" class="text-gray-600"> → </template></span>
      <span v-if="entry.type === 'file' && entry.size !== null" class="text-gray-600 flex-shrink-0 ml-1 tabular-nums">{{ formatSize(entry.size) }}</span>
    </div>

    <!-- Children (lazy loaded) -->
    <template v-if="entry.type === 'dir' && expanded">
      <div v-if="loadingChildren" class="text-xs text-gray-600" :style="{ paddingLeft: `${1.5 + depth * 1}rem` }">...</div>
      <div v-else-if="childError" class="text-xs text-red-500 px-2" :style="{ paddingLeft: `${1.5 + depth * 1}rem` }">{{ childError }}</div>
      <FileBrowserNode
        v-for="child in children"
        :key="child.name"
        :entry="child"
        :agent-id="agentId"
        :base-path="childBasePath"
        :depth="depth + 1"
        :expand-path="childExpandPath"
        :highlight-path="highlightPath"
        @file-select="$emit('file-select', $event)"
        @node-mounted="$emit('node-mounted', $event)"
      />
    </template>
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, nextTick } from 'vue'

const props = defineProps({
  entry: { type: Object, required: true },
  agentId: { type: String, required: true },
  basePath: { type: String, required: true },
  depth: { type: Number, default: 0 },
  // Array of remaining path segments to auto-expand, e.g. ['components', 'Foo.vue']
  expandPath: { type: Array, default: () => [] },
  // Full relative path of the file to highlight, e.g. 'src/components/Foo.vue'
  highlightPath: { type: String, default: '' },
})

const emit = defineEmits(['file-select', 'node-mounted'])

const rowEl = ref(null)
const expanded = ref(false)
const children = ref([])
const loadingChildren = ref(false)
const childError = ref('')

const entryPath = computed(() =>
  props.basePath ? `${props.basePath}/${props.entry.name}` : props.entry.name
)

const childBasePath = computed(() => entryPath.value)

// Whether this node is on the auto-expand path
const isOnExpandPath = computed(() =>
  props.expandPath.length > 0 && props.expandPath[0] === props.entry.name
)

// Remaining segments to pass down to children
const childExpandPath = computed(() =>
  isOnExpandPath.value ? props.expandPath.slice(1) : []
)

const isHighlighted = computed(() =>
  props.highlightPath ? entryPath.value === props.highlightPath : false
)

async function loadChildren() {
  loadingChildren.value = true
  childError.value = ''
  try {
    const res = await fetch(`/api/agents/${props.agentId}/files?path=${encodeURIComponent(entryPath.value)}`)
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      throw new Error(data.detail || `HTTP ${res.status}`)
    }
    const data = await res.json()
    children.value = data.entries
  } catch (e) {
    childError.value = e.message
  } finally {
    loadingChildren.value = false
  }
}

function onEntryClick() {
  if (props.entry.type === 'dir') {
    if (!expanded.value && children.value.length === 0 && !loadingChildren.value) {
      loadChildren()
    }
    expanded.value = !expanded.value
  } else {
    emit('file-select', { path: entryPath.value, size: props.entry.size ?? 0 })
  }
}

// Auto-expand when this node is on the expand path
watch(isOnExpandPath, async (val) => {
  if (val && props.entry.type === 'dir' && !expanded.value) {
    if (children.value.length === 0 && !loadingChildren.value) {
      await loadChildren()
    }
    expanded.value = true
  }
}, { immediate: true })

// Notify parent when the highlighted file node is mounted so it can scroll into view
onMounted(() => {
  if (isHighlighted.value && props.entry.type === 'file' && rowEl.value) {
    emit('node-mounted', { path: entryPath.value, el: rowEl.value })
  }
})

// Also watch for when highlighted becomes true after mount (children load async)
watch(isHighlighted, async (val) => {
  if (val && props.entry.type === 'file' && rowEl.value) {
    await nextTick()
    emit('node-mounted', { path: entryPath.value, el: rowEl.value })
  }
})

const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp', '.avif'])

function fileIcon(name) {
  const ext = name.includes('.') ? name.slice(name.lastIndexOf('.')).toLowerCase() : ''
  if (IMAGE_EXTS.has(ext)) return '🖼'
  return '📄'
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
</script>
