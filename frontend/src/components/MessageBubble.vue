<template>
  <div class="flex justify-start">
    <!-- System notice -->
    <div
      v-if="message.type === 'system'"
      class="w-full rounded-lg px-4 py-2 text-xs text-yellow-300 bg-yellow-900/30 border border-yellow-700/50"
    >
      ⚠ {{ message.content }}
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
      class="w-full rounded-lg px-4 py-2 text-sm"
      :class="bubbleClass"
    >
      <div
        v-if="message.role === 'agent' && !message.streaming"
        class="markdown-body"
        v-html="renderedContent"
      ></div>
      <div
        v-else-if="message.role === 'agent'"
        class="whitespace-pre-wrap"
      >{{ message.content }}</div>
      <div v-else class="whitespace-pre-wrap">{{ message.content }}</div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
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
  message: { type: Object, required: true },
})

const expanded = ref(false)

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

const renderedContent = computed(() => {
  if (!props.message.content) return ''
  if (props.message.streaming) return ''
  return md.render(props.message.content)
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

const toolDescription = computed(() => parsedToolInput.value?.description || '')
const toolCommand = computed(() => parsedToolInput.value?.command || '')
</script>
