<template>
  <div :class="message.role === 'user' ? 'flex justify-end' : 'flex justify-start'">
    <div
      class="max-w-2xl rounded-lg px-4 py-2 text-sm"
      :class="bubbleClass"
    >
      <div
        v-if="message.role === 'agent'"
        class="markdown-body"
        :class="{ 'streaming-cursor': message.streaming }"
        v-html="renderedContent"
      ></div>
      <div v-else class="whitespace-pre-wrap">{{ message.content }}</div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
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

const bubbleClass = computed(() =>
  props.message.role === 'user'
    ? 'bg-green-700/80 text-gray-100'
    : 'bg-gray-800 text-gray-200'
)

const renderedContent = computed(() => {
  if (!props.message.content) return ''
  return md.render(props.message.content)
})
</script>
