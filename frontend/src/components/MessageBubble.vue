<template>
  <div class="flex justify-start relative group">
    <!-- System notice -->
    <div
      v-if="message.type === 'system'"
      class="w-full rounded-lg px-4 py-2 text-xs text-yellow-300 bg-yellow-900/30 border border-yellow-700/50"
    >
      ⚠ {{ message.content }}
      <button
        v-if="!message.streaming"
        @click="copyContent"
        class="absolute top-1 right-2 opacity-0 group-hover:opacity-100 text-gray-500 hover:text-gray-200 transition-all p-1 text-xs"
        title="复制"
      >
        复制
      </button>
    </div>

    <!-- Write tool: show file path + markdown-rendered content -->
    <div
      v-if="message.type === 'tool_use' && isWriteTool"
      class="w-full rounded-lg px-4 py-2 text-sm bg-gray-800/60 border border-gray-700/50"
    >
      <div
        class="flex items-center gap-2 cursor-pointer select-none text-gray-400 hover:text-gray-300"
        @click="expanded = !expanded"
      >
        <span class="text-xs" :class="expanded ? 'rotate-90' : ''">&#9654;</span>
        <span class="font-mono text-xs text-blue-400">{{ message.tool_name }}</span>
        <span class="font-mono text-xs text-gray-400 truncate">{{ writeFilePath }}</span>
      </div>
      <div v-if="expanded" class="mt-2">
        <div class="markdown-body text-xs" v-html="writeContentRendered"></div>
      </div>
    </div>

    <!-- Tool use: show tool name + collapsible params -->
    <div
      v-else-if="message.type === 'tool_use'"
      class="w-full rounded-lg px-4 py-2 text-sm bg-gray-800/60 border border-gray-700/50"
    >
      <div
        class="flex items-center gap-2 min-w-0 cursor-pointer select-none text-gray-400 hover:text-gray-300"
        @click="expanded = !expanded"
      >
        <span class="text-xs shrink-0" :class="expanded ? 'rotate-90' : ''">&#9654;</span>
        <span class="font-mono text-xs text-blue-400 shrink-0">{{ message.tool_name }}</span>
        <span class="text-xs text-gray-500 shrink-0">tool call</span>
        <span v-if="toolDescription" class="text-xs text-gray-400 min-w-0 truncate">{{ toolDescription }}</span>
      </div>
      <div v-if="expanded" class="mt-2 text-xs">
        <pre class="whitespace-pre-wrap text-gray-400 overflow-x-auto max-h-60 overflow-y-auto">{{ formattedToolInput }}</pre>
      </div>
    </div>

    <!-- Tool result: show collapsible output -->
    <div
      v-else-if="message.type === 'tool_result'"
      class="w-full rounded-lg px-4 py-2 text-sm bg-gray-800/40 border border-gray-700/30"
    >
      <div
        class="flex items-center gap-2 cursor-pointer select-none text-gray-400 hover:text-gray-300"
        @click="expanded = !expanded"
      >
        <span class="text-xs" :class="expanded ? 'rotate-90' : ''">&#9654;</span>
        <span class="text-xs text-gray-500">tool result</span>
        <span class="text-xs text-gray-600">({{ contentPreview }})</span>
      </div>
      <div v-if="expanded" class="mt-2 text-xs">
        <pre class="whitespace-pre-wrap text-gray-400 overflow-x-auto max-h-80 overflow-y-auto">{{ message.content }}</pre>
      </div>
    </div>

    <!-- Regular text message -->
    <div
      v-else
      class="w-full rounded-lg px-4 py-2 text-sm relative group"
      :class="bubbleClass"
    >
      <div
        v-if="shouldCollapseLargeAgentMessage"
        class="space-y-3"
      >
        <div class="whitespace-pre-wrap max-h-64 overflow-hidden">{{ largeMessagePreview }}</div>
        <div class="flex items-center justify-between gap-3 text-xs text-gray-400">
          <span>{{ largeMessageSummary }}</span>
          <button class="text-blue-400 hover:text-blue-300" @click="messageExpanded = true">Render full message</button>
        </div>
      </div>
      <div
        v-else-if="message.role === 'agent' && !message.streaming"
        class="markdown-body"
        v-html="renderedContent"
      ></div>
      <div
        v-else-if="message.role === 'agent'"
        class="whitespace-pre-wrap"
      >{{ message.content }}</div>
      <div v-else class="whitespace-pre-wrap">{{ message.content }}</div>

      <button
        v-if="!message.streaming"
        @click="copyContent"
        class="absolute top-1 right-2 opacity-0 group-hover:opacity-100 text-gray-500 hover:text-gray-200 transition-all p-1 text-xs"
        title="复制"
      >
        复制
      </button>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import { useAgentStore } from '../stores/agentStore'

const LARGE_MESSAGE_CHAR_LIMIT = 12000
const LARGE_MESSAGE_LINE_LIMIT = 400
const LARGE_MESSAGE_PREVIEW_CHAR_LIMIT = 4000
const LARGE_MESSAGE_PREVIEW_LINE_LIMIT = 120
const markdownRenderCache = new WeakMap()

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

function getLineCount(content) {
  if (!content) return 0
  return content.split('\n').length
}

function buildLargeMessagePreview(content) {
  const lines = content.split('\n').slice(0, LARGE_MESSAGE_PREVIEW_LINE_LIMIT)
  let preview = lines.join('\n')
  if (preview.length > LARGE_MESSAGE_PREVIEW_CHAR_LIMIT) {
    preview = preview.slice(0, LARGE_MESSAGE_PREVIEW_CHAR_LIMIT)
  }
  return `${preview}\n\n...`
}

function formatMessageSize(length) {
  if (length < 1024) return `${length} chars`
  return `${(length / 1024).toFixed(1)} KB`
}

function renderMarkdownCached(message) {
  const cached = markdownRenderCache.get(message)
  if (cached && cached.content === message.content) {
    return cached.html
  }

  const html = md.render(message.content)
  markdownRenderCache.set(message, { content: message.content, html })
  return html
}

const props = defineProps({
  message: { type: Object, required: true },
})

const store = useAgentStore()
const expanded = ref(false)
const messageExpanded = ref(false)

const parsedToolInput = computed(() => {
  try {
    return JSON.parse(props.message.content)
  } catch {
    return null
  }
})

const isWriteTool = computed(() => {
  const name = (props.message.tool_name || '').toLowerCase()
  return name === 'write' && parsedToolInput.value?.file_path && parsedToolInput.value?.content
})

const writeFilePath = computed(() => parsedToolInput.value?.file_path || '')

const writeContentRendered = computed(() => {
  const content = parsedToolInput.value?.content || ''
  return md.render(content)
})

const bubbleClass = computed(() =>
  props.message.role === 'user'
    ? 'bg-green-700/80 text-gray-100'
    : 'bg-gray-800 text-gray-200'
)

const messageLineCount = computed(() => getLineCount(props.message.content || ''))

const isLargeAgentMessage = computed(() =>
  props.message.role === 'agent' &&
  !props.message.streaming &&
  (
    (props.message.content || '').length > LARGE_MESSAGE_CHAR_LIMIT ||
    messageLineCount.value > LARGE_MESSAGE_LINE_LIMIT
  )
)

const shouldCollapseLargeAgentMessage = computed(() =>
  isLargeAgentMessage.value && !messageExpanded.value
)

const largeMessagePreview = computed(() =>
  buildLargeMessagePreview(props.message.content || '')
)

const largeMessageSummary = computed(() =>
  `${formatMessageSize((props.message.content || '').length)} • ${messageLineCount.value} lines`
)

const renderedContent = computed(() => {
  if (!props.message.content) return ''
  if (props.message.streaming) return ''
  if (shouldCollapseLargeAgentMessage.value) return ''
  return renderMarkdownCached(props.message)
})

const formattedToolInput = computed(() => {
  try {
    const parsed = JSON.parse(props.message.content)
    return JSON.stringify(parsed, null, 2)
  } catch {
    return props.message.content
  }
})

const contentPreview = computed(() => {
  const c = props.message.content || ''
  const firstLine = c.split('\n')[0]
  if (firstLine.length > 80) return firstLine.slice(0, 80) + '...'
  return firstLine
})

async function copyContent() {
  try {
    await navigator.clipboard.writeText(props.message.content)
    store.addToast('已复制到剪贴板', 'success')
  } catch (err) {
    store.addToast('复制失败', 'error')
  }
}
</script>
