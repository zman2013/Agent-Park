// Shared browser notification + tab-title flash helper.
//
// Why a module-level singleton: task_status (WebSocket) and agentloop polling
// are two independent sources that both want to flash the tab title. If each
// owned its own timer + originalTitle snapshot, the second source could
// capture the first's flashing title as its "original", leaving the title
// stuck in the bracketed alert form after focus returns.

const ORIGINAL_TITLE = document.title
let flashTimer = null
let focusHandlerBound = false

function stopTitleFlash() {
  if (flashTimer) {
    clearInterval(flashTimer)
    flashTimer = null
    document.title = ORIGINAL_TITLE
  }
}

function bindFocusHandlerOnce() {
  if (focusHandlerBound) return
  focusHandlerBound = true
  window.addEventListener('focus', () => {
    stopTitleFlash()
  })
}

function startTitleFlash(alertText) {
  // Restart cleanly if already flashing so a newer event replaces the message.
  stopTitleFlash()
  let show = true
  flashTimer = setInterval(() => {
    document.title = show ? alertText : ORIGINAL_TITLE
    show = !show
  }, 800)
  bindFocusHandlerOnce()
}

export function notify(title, body) {
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
