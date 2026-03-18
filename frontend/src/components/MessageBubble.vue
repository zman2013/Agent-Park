<template>
  <div class="flex justify-start">
    <!-- Tool use: show tool name + collapsible params -->
    <div
      v-if="message.type === 'tool_use'"
      class="w-full rounded-lg px-4 py-2 text-sm bg-gray-800/60 border border-gray-700/50"
    >
      <div
        class="flex items-center gap-2 cursor-pointer select-none text-gray-400 hover:text-gray-300"
        @click="expanded = !expanded"
      >
        <span class="text-xs" :class="expanded ? 'rotate-90' : ''">&#9654;</span>
        <span class="font-mono text-xs text-blue-400">{{ message.tool_name }}</span>
        <span class="text-xs text-gray-500">tool call</span>
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
        v-if="message.role === 'agent'"
        class="markdown-body"
        v-html="renderedContent"
      ></div>
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

const bubbleClass = computed(() =>
  props.message.role === 'user'
    ? 'bg-green-700/80 text-gray-100'
    : 'bg-gray-800 text-gray-200'
)

const renderedContent = computed(() => {
  if (!props.message.content) return ''
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
</script>
