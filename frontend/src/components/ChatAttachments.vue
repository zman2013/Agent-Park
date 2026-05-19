<template>
  <div v-if="attachments.length > 0" class="flex flex-wrap gap-2 mb-2">
    <div
      v-for="att in attachments"
      :key="att.uid"
      class="flex items-center gap-2 bg-[#1a1a1a] border rounded-lg px-2.5 py-1.5 text-xs max-w-full"
      :class="borderClass(att)"
    >
      <span class="shrink-0">{{ icon(att) }}</span>
      <span class="truncate text-gray-200" :title="att.name">{{ att.name }}</span>
      <span v-if="att.size != null" class="shrink-0 text-gray-500">{{ formatSize(att.size) }}</span>

      <!-- Uploading: progress bar -->
      <div
        v-if="att.status === 'uploading'"
        class="shrink-0 w-20 h-1.5 bg-gray-700 rounded overflow-hidden"
      >
        <div
          class="h-full bg-blue-500 transition-all"
          :style="{ width: att.progress + '%' }"
        ></div>
      </div>
      <span v-else-if="att.status === 'success'" class="shrink-0 text-green-400">✓</span>
      <span
        v-else-if="att.status === 'failed'"
        class="shrink-0 text-red-400"
        :title="att.error || 'upload failed'"
      >⚠</span>

      <button
        class="shrink-0 text-gray-500 hover:text-gray-200 transition-colors"
        :title="att.status === 'uploading' ? 'Cancel upload' : 'Remove'"
        @click="$emit('remove', att.uid)"
      >×</button>
    </div>
  </div>
</template>

<script setup>
defineProps({
  attachments: { type: Array, required: true },
})
defineEmits(['remove'])

function icon(att) {
  return '\u{1F4C4}' // 📄
}

function borderClass(att) {
  if (att.status === 'uploading') return 'border-blue-500/40'
  if (att.status === 'success') return 'border-green-500/40'
  if (att.status === 'failed') return 'border-red-500/40'
  return 'border-gray-700'
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB'
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB'
}
</script>
