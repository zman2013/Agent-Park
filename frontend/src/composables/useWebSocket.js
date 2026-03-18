import { ref, onMounted, onUnmounted } from 'vue'
import { useAgentStore } from '../stores/agentStore'

export function useWebSocket() {
  const store = useAgentStore()
  const connected = ref(false)
  let ws = null
  let reconnectTimer = null

  function connect() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${location.host}/ws`
    console.log('[WS] connecting to', url)

    ws = new WebSocket(url)

    ws.onopen = () => {
      connected.value = true
      console.log('[WS] connected')
    }

    ws.onclose = (e) => {
      connected.value = false
      console.log('[WS] closed', e.code, e.reason)
      // Auto-reconnect
      reconnectTimer = setTimeout(connect, 2000)
    }

    ws.onerror = (e) => {
      console.error('[WS] error', e)
      ws.close()
    }

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      console.log('[WS] recv', data.type, data)
      handleMessage(data)
    }
  }

  function handleMessage(data) {
    switch (data.type) {
      case 'state_sync':
        store.syncState(data.data)
        break

      case 'task_status':
        store.updateTaskStatus(data.task_id, data.status)
        if (data.status === 'success') {
          store.addToast(`Task completed`, 'success')
        } else if (data.status === 'failed') {
          store.addToast(`Task failed`, 'error')
        } else if (data.status === 'waiting') {
          store.addToast(`Agent is waiting for your input`, 'warning')
        }
        break

      case 'message':
        store.addMessage(data.task_id, data.message)
        break

      case 'message_chunk':
        store.appendChunk(data.task_id, data.message_id, data.delta)
        break

      case 'message_done':
        store.markMessageDone(data.task_id, data.message_id)
        break
    }
  }

  function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data))
    }
  }

  function createTask(agentId, prompt) {
    send({ type: 'create_task', agent_id: agentId, prompt })
  }

  function sendUserMessage(taskId, content) {
    send({ type: 'user_message', task_id: taskId, content })
  }

  onMounted(() => {
    connect()
  })

  onUnmounted(() => {
    clearTimeout(reconnectTimer)
    if (ws) ws.close()
  })

  return {
    connected,
    createTask,
    sendUserMessage,
  }
}
