<template>
  <div class="space-y-2">
    <div
      v-for="(line, idx) in lines"
      :key="idx"
      class="rounded border text-xs"
      :class="containerClass(line)"
    >
      <!-- system init -->
      <div v-if="isSystemInit(line)" class="px-3 py-1.5 text-gray-500">
        <span class="text-blue-400 font-mono">system</span>
        <span v-if="line.session_id" class="ml-2 text-gray-600">session: {{ line.session_id }}</span>
        <span v-if="line.model" class="ml-2 text-gray-600">model: {{ line.model }}</span>
      </div>

      <!-- assistant text -->
      <div v-else-if="isAssistantText(line)" class="px-3 py-2 text-gray-200 whitespace-pre-wrap">
        {{ extractText(line) }}
      </div>

      <!-- tool_use -->
      <div v-else-if="isToolUse(line)" class="px-3 py-1.5">
        <div
          class="flex items-center gap-2 cursor-pointer select-none text-gray-400 hover:text-gray-300"
          @click="toggle(idx)"
        >
          <span class="text-xs shrink-0" :class="expanded[idx] ? 'rotate-90' : ''">&#9654;</span>
          <span class="font-mono text-xs text-blue-400 shrink-0">{{ toolName(line) }}</span>
          <span class="text-xs text-gray-500 shrink-0">tool call</span>
          <span v-if="toolDescription(line)" class="text-xs text-gray-400 min-w-0 truncate">{{ toolDescription(line) }}</span>
        </div>
        <div v-if="expanded[idx]" class="mt-2">
          <pre class="whitespace-pre-wrap text-gray-400 overflow-x-auto max-h-60 overflow-y-auto">{{ prettyJson(toolInput(line)) }}</pre>
        </div>
      </div>

      <!-- tool_result (usually surfaced as user-role message with content=tool_result) -->
      <div v-else-if="isToolResult(line)" class="px-3 py-1.5">
        <div
          class="flex items-center gap-2 cursor-pointer select-none text-gray-400 hover:text-gray-300"
          @click="toggle(idx)"
        >
          <span class="text-xs" :class="expanded[idx] ? 'rotate-90' : ''">&#9654;</span>
          <span class="text-xs text-gray-500">tool result</span>
          <span class="text-xs text-gray-600">({{ previewText(toolResultText(line)) }})</span>
        </div>
        <div v-if="expanded[idx]" class="mt-2">
          <pre class="whitespace-pre-wrap text-gray-400 overflow-x-auto max-h-80 overflow-y-auto">{{ toolResultText(line) }}</pre>
        </div>
      </div>

      <!-- final result block -->
      <div v-else-if="line.type === 'result'" class="px-3 py-2 text-gray-300">
        <span class="font-mono text-xs" :class="line.subtype === 'success' ? 'text-green-400' : 'text-red-400'">
          result: {{ line.subtype || 'unknown' }}
        </span>
        <span v-if="line.total_cost_usd !== undefined" class="ml-3 text-gray-500">${{ line.total_cost_usd.toFixed(4) }}</span>
        <span v-if="line.duration_ms !== undefined" class="ml-3 text-gray-500">{{ (line.duration_ms / 1000).toFixed(1) }}s</span>
        <span v-if="line.num_turns !== undefined" class="ml-3 text-gray-500">{{ line.num_turns }} turns</span>
      </div>

      <!-- fallback: raw JSON -->
      <div v-else class="px-3 py-1.5">
        <div
          class="flex items-center gap-2 cursor-pointer select-none text-gray-500 hover:text-gray-400"
          @click="toggle(idx)"
        >
          <span class="text-xs" :class="expanded[idx] ? 'rotate-90' : ''">&#9654;</span>
          <span class="font-mono text-xs">{{ line.type || '?' }}</span>
          <span v-if="line.subtype" class="text-xs text-gray-600">· {{ line.subtype }}</span>
        </div>
        <div v-if="expanded[idx]" class="mt-2">
          <pre class="whitespace-pre-wrap text-gray-500 overflow-x-auto max-h-60 overflow-y-auto text-xs">{{ prettyJson(line) }}</pre>
        </div>
      </div>
    </div>
    <div v-if="!lines.length" class="text-gray-600 text-xs px-2">（无数据）</div>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  lines: { type: Array, required: true, default: () => [] },
})

const expanded = ref({})

function toggle(idx) {
  expanded.value[idx] = !expanded.value[idx]
}

// Only reset expansion state when the log is truncated/replaced (shorter or
// first line changed). Appending new lines — which happens on every 3s poll —
// must preserve the user's manually-expanded tool-call boxes.
watch(
  () => props.lines,
  (nv, ov) => {
    if (!nv || !ov) return
    const shrunk = nv.length < ov.length
    const firstChanged = nv.length > 0 && ov.length > 0 && nv[0] !== ov[0]
    if (shrunk || firstChanged) {
      expanded.value = {}
    }
  }
)

// ── stream-json shape helpers ────────────────────────────────────────────
// cco/ccs emit chunks loosely matching Claude's streaming format:
//   {"type": "system", "subtype": "init", ...}
//   {"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
//   {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "...", "input": {...}}]}}
//   {"type": "user", "message": {"content": [{"type": "tool_result", "content": "..."}]}}
//   {"type": "result", "subtype": "success", ...}

function isSystemInit(line) {
  return line && line.type === 'system'
}

function contentBlocks(line) {
  return line?.message?.content || []
}

function isAssistantText(line) {
  if (line?.type !== 'assistant') return false
  return contentBlocks(line).some(b => b && b.type === 'text')
}

function extractText(line) {
  return contentBlocks(line)
    .filter(b => b && b.type === 'text')
    .map(b => b.text || '')
    .join('\n')
}

function isToolUse(line) {
  if (line?.type !== 'assistant') return false
  return contentBlocks(line).some(b => b && b.type === 'tool_use')
}

function toolBlock(line) {
  return contentBlocks(line).find(b => b && b.type === 'tool_use') || {}
}

function toolName(line) {
  return toolBlock(line).name || '?'
}

function toolInput(line) {
  return toolBlock(line).input || {}
}

function toolDescription(line) {
  const input = toolInput(line)
  return input.description || ''
}

function isToolResult(line) {
  if (line?.type !== 'user') return false
  return contentBlocks(line).some(b => b && b.type === 'tool_result')
}

function toolResultText(line) {
  const block = contentBlocks(line).find(b => b && b.type === 'tool_result')
  if (!block) return ''
  if (typeof block.content === 'string') return block.content
  if (Array.isArray(block.content)) {
    return block.content
      .map(c => (typeof c === 'string' ? c : c.text || JSON.stringify(c)))
      .join('\n')
  }
  return JSON.stringify(block.content || '')
}

function previewText(text) {
  const firstLine = (text || '').split('\n')[0] || ''
  return firstLine.length > 80 ? firstLine.slice(0, 80) + '…' : firstLine
}

function prettyJson(obj) {
  try {
    return JSON.stringify(obj, null, 2)
  } catch {
    return String(obj)
  }
}

function containerClass(line) {
  if (line?.type === 'assistant' && contentBlocks(line).some(b => b && b.type === 'tool_use')) {
    return 'bg-gray-800/60 border-gray-700/50'
  }
  if (line?.type === 'user') return 'bg-gray-800/40 border-gray-700/30'
  if (line?.type === 'result') return 'bg-gray-800/40 border-gray-700/30'
  if (line?.type === 'system') return 'bg-gray-800/30 border-gray-700/20'
  return 'bg-transparent border-transparent'
}
</script>
