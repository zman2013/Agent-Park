<template>
  <div class="fixed top-4 right-4 z-50 space-y-2">
    <div
      v-for="toast in store.toasts"
      :key="toast.id"
      class="toast-enter px-4 py-2 rounded-lg text-sm shadow-lg max-w-xs"
      :class="toastClass(toast.type)"
    >
      {{ toast.text }}
    </div>
  </div>
</template>

<script setup>
import { useAgentStore } from '../stores/agentStore'

const store = useAgentStore()

function toastClass(type) {
  switch (type) {
    case 'success': return 'bg-green-800/90 text-green-200'
    case 'error': return 'bg-red-800/90 text-red-200'
    case 'warning': return 'bg-yellow-800/90 text-yellow-200'
    default: return 'bg-gray-800/90 text-gray-200'
  }
}
</script>
