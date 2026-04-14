<template>
  <div class="flex-1 flex flex-col overflow-hidden min-h-0">
    <!-- Title bar / breadcrumb -->
    <div class="flex items-center gap-2 px-4 py-2 border-b border-gray-800 text-xs text-gray-400 flex-shrink-0">
      <button class="hover:text-gray-200 transition-colors" @click="$emit('close')">Chat</button>
      <span class="text-gray-600">/</span>
      <span class="font-mono truncate flex-1 min-w-0 text-gray-300" :title="filePath">{{ filePath }}</span>
      <span v-if="displaySize" class="text-gray-600 flex-shrink-0 tabular-nums">{{ displaySize }}</span>
      <!-- Word wrap toggle: only shown for plain text -->
      <button
        v-if="content !== null && !isMarkdown && !isImage"
        class="flex-shrink-0 transition-colors px-1"
        :class="wordWrap ? 'text-gray-300' : 'text-gray-600 hover:text-gray-400'"
        :title="wordWrap ? '关闭自动换行' : '开启自动换行'"
        @click="wordWrap = !wordWrap"
      >⇌</button>
      <button
        class="text-gray-600 hover:text-gray-300 transition-colors flex-shrink-0 ml-1"
        title="Close preview"
        @click="$emit('close')"
      >×</button>
    </div>

    <!-- Content area -->
    <div class="flex-1 overflow-auto min-h-0">
      <!-- Loading -->
      <div v-if="loading" class="flex items-center justify-center h-full text-xs text-gray-600">Loading...</div>

      <!-- Large file confirm UI -->
      <div v-else-if="showLargeConfirm" class="flex flex-col items-center justify-center h-full gap-4 text-sm text-gray-400">
        <div>文件较大 ({{ displaySize }})，是否加载内容？</div>
        <div class="flex gap-3">
          <button
            class="px-4 py-1.5 text-xs bg-gray-700 hover:bg-gray-600 rounded transition-colors"
            @click="loadContent(true)"
          >显示内容</button>
          <a
            :href="downloadUrl"
            download
            class="px-4 py-1.5 text-xs bg-gray-700 hover:bg-gray-600 rounded transition-colors"
          >下载文件</a>
        </div>
      </div>

      <!-- Binary file -->
      <div v-else-if="isBinary" class="flex flex-col items-center justify-center h-full gap-4 text-sm text-gray-400">
        <div>二进制文件，无法显示内容</div>
        <a
          :href="downloadUrl"
          download
          class="px-4 py-1.5 text-xs bg-gray-700 hover:bg-gray-600 rounded transition-colors"
        >下载文件</a>
      </div>

      <!-- Error -->
      <div v-else-if="loadError" class="px-4 py-4 text-xs text-red-400">{{ loadError }}</div>

      <!-- Image preview -->
      <div v-else-if="isImage" class="flex items-center justify-center h-full bg-gray-950 p-4">
        <img :src="downloadUrl" class="max-w-full max-h-full object-contain" :alt="filePath" />
      </div>

      <!-- Markdown content -->
      <div
        v-else-if="content !== null && isMarkdown"
        class="prose prose-sm prose-invert max-w-none p-6"
        v-html="renderedMarkdown"
      ></div>

      <!-- Plain text content -->
      <pre
        v-else-if="content !== null"
        class="p-4 text-xs font-mono text-gray-300 overflow-auto leading-relaxed"
        :class="wordWrap ? 'whitespace-pre-wrap break-all' : 'whitespace-pre'"
      >{{ content }}</pre>

      <!-- Initial state: image or large file check triggered by watcher -->
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'

const md = new MarkdownIt({
  html: false,
  linkify: true,
  highlight(str, lang) {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return hljs.highlight(str, { language: lang }).value
      } catch (_) {}
    }
    return ''
  },
})

const props = defineProps({
  agentId: { type: String, required: true },
  filePath: { type: String, required: true },
  fileSize: { type: Number, default: 0 },
})

defineEmits(['close'])

const content = ref(null)
const loading = ref(false)
const loadError = ref('')
const showLargeConfirm = ref(false)
const isBinary = ref(false)
const wordWrap = ref(true)

const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp', '.avif'])

const fileExt = computed(() => {
  const name = props.filePath
  return name.includes('.') ? name.slice(name.lastIndexOf('.')).toLowerCase() : ''
})

const isImage = computed(() => IMAGE_EXTS.has(fileExt.value))

const isMarkdown = computed(() => fileExt.value === '.md' || fileExt.value === '.markdown')

const renderedMarkdown = computed(() => {
  if (!content.value || !isMarkdown.value) return ''
  return md.render(content.value)
})

const displaySize = computed(() => {
  const bytes = props.fileSize
  if (!bytes) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
})

const downloadUrl = computed(() =>
  `/api/agents/${props.agentId}/files/download?path=${encodeURIComponent(props.filePath)}`
)

async function loadContent(force = false) {
  // Images: rendered via <img> directly
  if (isImage.value) return

  const LARGE = 1024 * 1024
  if (!force && props.fileSize >= LARGE) {
    showLargeConfirm.value = true
    return
  }

  showLargeConfirm.value = false
  isBinary.value = false
  loading.value = true
  loadError.value = ''
  content.value = null

  try {
    const res = await fetch(`/api/agents/${props.agentId}/files/content?path=${encodeURIComponent(props.filePath)}`)
    if (res.status === 413) {
      showLargeConfirm.value = true
      return
    }
    if (res.status === 415) {
      isBinary.value = true
      return
    }
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      throw new Error(data.detail || `HTTP ${res.status}`)
    }
    content.value = await res.text()
  } catch (e) {
    loadError.value = e.message
  } finally {
    loading.value = false
  }
}

watch(
  () => props.filePath,
  () => {
    content.value = null
    loadError.value = ''
    showLargeConfirm.value = false
    isBinary.value = false
    wordWrap.value = false
    loadContent()
  },
  { immediate: true }
)
</script>
