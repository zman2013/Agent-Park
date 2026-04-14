<template>
  <div v-if="recentFiles.length > 0" class="border-b border-gray-800 pb-2 mb-1 max-h-[40vh] flex flex-col">
    <div class="flex items-center justify-between px-4 py-1.5 flex-shrink-0">
      <span class="text-xs text-gray-500 uppercase tracking-wider font-semibold">近期文件</span>
      <span class="text-xs text-gray-600 bg-gray-800 rounded-full px-1.5 py-0.5 tabular-nums">{{ recentFiles.length }}</span>
    </div>
    <div class="overflow-auto flex-1">
      <div class="min-w-fit">
      <div
        v-for="f in recentFiles"
        :key="f.path"
        class="flex items-center gap-2 px-4 py-1.5 cursor-pointer rounded text-sm transition-colors hover:bg-gray-800/50 group"
        @click="handleClick(f)"
      >
        <span class="text-xs text-gray-600 flex-shrink-0">📄</span>
        <span class="whitespace-nowrap flex-1 text-gray-300" :title="f.path">{{ f.name }}</span>
        <button
          class="text-gray-600 hover:text-gray-300 text-xs flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
          title="关闭"
          @click.stop="handleRemove(f.path)"
        >×</button>
      </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useAgentStore } from '../stores/agentStore'

const props = defineProps({
  agentId: { type: String, required: true },
})

const emit = defineEmits(['file-select'])

const store = useAgentStore()

const recentFiles = computed(() =>
  store.getRecentFiles(props.agentId).slice(0, 10)
)

function handleClick(f) {
  emit('file-select', { path: f.path })
}

function handleRemove(path) {
  store.removeRecentFile(props.agentId, path)
}
</script>
