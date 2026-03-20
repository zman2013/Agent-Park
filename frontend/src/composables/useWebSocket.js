import { ref, onMounted, onUnmounted } from 'vue'
import { useAgentStore } from '../stores/agentStore'

const CHUNK_FLUSH_INTERVAL_MS = 100

export function useWebSocket() {
  const store = useAgentStore()
  const connected = ref(false)
  let ws = null
  let reconnectTimer = null
  let chunkFlushTimer = null
  const pendingChunks = new Map()

  function getChunkKey(taskId, messageId) {
    return `${taskId}:${messageId}`
  }

  function flushPendingChunks() {
    if (chunkFlushTimer) {
      clearTimeout(chunkFlushTimer)
      chunkFlushTimer = null
    }
    if (pendingChunks.size === 0) return

    const chunks = Array.from(pendingChunks.values())
    pendingChunks.clear()
    store.appendChunks(chunks)
  }

  function queueChunk(taskId, messageId, delta) {
    const key = getChunkKey(taskId, messageId)
    const existing = pendingChunks.get(key)

    if (existing) {
      existing.delta += delta
    } else {
      pendingChunks.set(key, { taskId, messageId, delta })
    }

    if (!chunkFlushTimer) {
      chunkFlushTimer = setTimeout(flushPendingChunks, CHUNK_FLUSH_INTERVAL_MS)
    }
  }

  function connect() {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${location.host}/ws`
    console.log('[WS] connecting to', url)

    ws = new WebSocket(url)

    ws.onopen = () => {
      connected.value = true
      console.log('[WS] connected')
      // Request browser notification permission on first connect
      if ('Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission()
      }
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
      handleMessage(data)
    }
  }

  // ── Title-flash fallback for non-secure contexts (HTTP remote IP) ──
  const originalTitle = document.title
  let titleFlashTimer = null

  function startTitleFlash(alertText) {
    stopTitleFlash()
    let show = true
    titleFlashTimer = setInterval(() => {
      document.title = show ? alertText : originalTitle
      show = !show
    }, 800)
    // Stop flashing when user comes back to the tab
    const onFocus = () => {
      stopTitleFlash()
      window.removeEventListener('focus', onFocus)
    }
    window.addEventListener('focus', onFocus)
  }

  function stopTitleFlash() {
    if (titleFlashTimer) {
      clearInterval(titleFlashTimer)
      titleFlashTimer = null
      document.title = originalTitle
    }
  }

  function sendBrowserNotify(title, body) {
    // Native Notification (only works in secure context: HTTPS / localhost)
    if (
      'Notification' in window &&
      Notification.permission === 'granted' &&
      document.hidden
    ) {
      const n = new Notification(title, { body })
      n.onclick = () => {
        window.focus()
        n.close()
      }
    }
    // Title flash fallback (works everywhere, only when tab is hidden)
    if (document.hidden) {
      startTitleFlash(`【${title}】${body}`)
    }
  }

  function handleMessage(data) {
    switch (data.type) {
      case 'state_sync': {
        flushPendingChunks()
        // Detect newly added tasks before syncing
        const prevTaskIds = new Set(Object.keys(store.tasks))
        store.syncState(data.data)
        // If exactly one new task appeared, auto-select it
        const newTaskIds = Object.keys(store.tasks).filter(id => !prevTaskIds.has(id))
        if (newTaskIds.length === 1) {
          store.selectTask(newTaskIds[0])
        }
        break
      }

      case 'task_status': {
        store.updateTaskStatus(data.task_id, data.status)
        const taskName = store.tasks[data.task_id]?.name || data.task_id
        if (data.status === 'success') {
          store.addToast(`Task completed`, 'success')
          sendBrowserNotify('任务完成', taskName)
        } else if (data.status === 'failed') {
          store.addToast(`Task failed`, 'error')
          sendBrowserNotify('任务失败', taskName)
        } else if (data.status === 'waiting') {
          store.addToast(`Agent is waiting for your input`, 'warning')
          sendBrowserNotify('等待输入', taskName)
        }
        break
      }

      case 'message':
        store.addMessage(data.task_id, data.message)
        break

      case 'message_chunk':
        queueChunk(data.task_id, data.message_id, data.delta)
        break

      case 'message_done':
        flushPendingChunks()
        store.markMessageDone(data.task_id, data.message_id)
        break

      case 'turns_info':
        store.updateTaskTurns(data.task_id, data.num_turns)
        break

      case 'agent_updated':
        store.updateAgent(data.agent_id, data.fields)
        break
    }
  }

  function send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(data))
    }
  }

  function createTask(agentId, name) {
    send({ type: 'create_task', agent_id: agentId, name })
  }

  function sendUserMessage(taskId, content, command = null) {
    const payload = { type: 'user_message', task_id: taskId, content };
    if (command) {
      payload.command = command;
    }
    send(payload);
  }

  onMounted(() => {
    connect()
  })

  onUnmounted(() => {
    flushPendingChunks()
    clearTimeout(reconnectTimer)
    if (ws) ws.close()
  })

  return {
    connected,
    createTask,
    sendUserMessage,
  }
}
