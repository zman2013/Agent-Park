<template>
  <div class="bg-[#111] border-r border-gray-800 flex flex-col h-full overflow-hidden">
    <div class="p-4 text-xs text-gray-500 uppercase tracking-wider font-semibold">
      Agents
    </div>
    <div class="flex-1 overflow-auto px-2 pb-4">
      <AgentGroup
        v-for="agent in store.agents"
        :key="agent.id"
        :agent="agent"
      />
    </div>
  </div>
</template>

<script setup>
import { useAgentStore } from '../stores/agentStore'
import AgentGroup from './AgentGroup.vue'

const store = useAgentStore()
</script>
