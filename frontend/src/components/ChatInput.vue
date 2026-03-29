<template>
  <div class="border-t border-gray-800 p-3 relative">
    <div
      v-if="task.status === 'waiting'"
      class="text-xs text-blue-400 mb-2 px-1"
    >
      Agent is waiting for your input...
    </div>

    <!-- Skill autocomplete dropdown -->
    <div
      v-if="showSkillMenu && filteredSkills.length > 0"
      class="absolute bottom-full left-3 right-3 mb-1 bg-[#1a1a1a] border border-gray-700 rounded-lg overflow-hidden shadow-lg z-10 max-h-64 overflow-y-auto"
    >
      <div
        v-for="(skill, idx) in filteredSkills"
        :key="skill.name"
        class="flex items-baseline gap-2 px-3 py-2 cursor-pointer hover:bg-gray-800 transition-colors"
        :class="{ 'bg-gray-800': idx === activeIndex }"
        @mousedown.prevent="selectSkill(skill)"
      >
        <span class="text-blue-400 font-mono text-sm shrink-0">/{{ skill.name }}</span>
        <span class="text-gray-400 text-xs truncate">{{ skill.description }}</span>
      </div>
    </div>

    <div class="flex gap-2">
      <textarea
        ref="inputEl"
        v-model="text"
        class="flex-1 bg-[#111] border border-gray-700 p-2.5 rounded-lg outline-none text-sm resize-none focus:border-gray-500 transition-colors"
        :class="{ 'border-blue-500/50': task.status === 'waiting' }"
        placeholder="Reply to agent..."
        rows="1"
        @keydown="handleKeydown"
        @input="handleInput"
        @blur="handleBlur"
      ></textarea>
      <button
        class="bg-green-600 hover:bg-green-700 px-4 rounded-lg text-sm font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        :disabled="!text.trim()"
        @click="send"
      >
        Send
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, watch } from 'vue'

const props = defineProps({
  task: { type: Object, required: true },
})

const emit = defineEmits(['send'])
const text = ref('')
const inputEl = ref(null)

// Skill autocomplete state
const allSkills = ref([])
const showSkillMenu = ref(false)
const activeIndex = ref(0)

// localStorage key for this task's draft
const draftKey = computed(() => `chat_draft_${props.task.id}`)

// Debounce timer for saving draft
let saveTimer = null
function saveDraft(val) {
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => {
    if (val) {
      localStorage.setItem(draftKey.value, val)
    } else {
      localStorage.removeItem(draftKey.value)
    }
  }, 500)
}

// Watch text changes and persist with debounce
watch(text, (val) => {
  saveDraft(val)
})

onMounted(async () => {
  // Restore draft from localStorage
  const saved = localStorage.getItem(draftKey.value)
  if (saved) {
    text.value = saved
    nextTick(() => autoResize())
  }

  try {
    const res = await fetch('/api/skills')
    if (res.ok) {
      allSkills.value = await res.json()
    }
  } catch {
    // ignore
  }
})

const filteredSkills = computed(() => {
  if (!showSkillMenu.value) return []
  const query = text.value
  if (!query.startsWith('/')) return []
  const prefix = query.slice(1).toLowerCase()
  if (prefix === '') return allSkills.value
  return allSkills.value.filter(s => s.name.toLowerCase().startsWith(prefix))
})

function handleInput() {
  autoResize()
  const val = text.value
  if (val.startsWith('/') && !val.includes(' ')) {
    showSkillMenu.value = true
    activeIndex.value = 0
  } else {
    showSkillMenu.value = false
  }
}

function handleBlur() {
  // Delay to allow mousedown on dropdown items to fire first
  setTimeout(() => {
    showSkillMenu.value = false
  }, 150)
}

function selectSkill(skill) {
  text.value = '/' + skill.name + ' '
  showSkillMenu.value = false
  nextTick(() => {
    autoResize()
    inputEl.value?.focus()
  })
}

function handleKeydown(e) {
  if (showSkillMenu.value && filteredSkills.value.length > 0) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      activeIndex.value = (activeIndex.value + 1) % filteredSkills.value.length
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      activeIndex.value = (activeIndex.value - 1 + filteredSkills.value.length) % filteredSkills.value.length
      return
    }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
      e.preventDefault()
      selectSkill(filteredSkills.value[activeIndex.value])
      return
    }
    if (e.key === 'Escape') {
      showSkillMenu.value = false
      return
    }
  }

  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

function autoResize() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 150) + 'px'
}

function send() {
  const content = text.value.trim()
  if (!content) return

  window.dispatchEvent(new CustomEvent('send-message', {
    detail: { taskId: props.task.id, content }
  }))

  text.value = ''
  clearTimeout(saveTimer)
  localStorage.removeItem(draftKey.value)
  showSkillMenu.value = false
  nextTick(() => autoResize())
}
</script>
