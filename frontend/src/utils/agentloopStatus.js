// Map agentloop status → Tailwind text color. Centralized so header bar,
// recent sidebar, and workspace drawer all stay in sync.
export function agentloopStatusColor(status) {
  switch (status) {
    case 'running': return 'text-yellow-400'
    case 'done': return 'text-green-500'
    case 'exhausted': return 'text-orange-400'
    case 'stopped': return 'text-gray-500'
    // Paused pending human review — amber reads as "needs you", distinct from
    // running (yellow) and from failure (orange/red).
    case 'awaiting_review': return 'text-amber-300'
    case 'plan_rejected': return 'text-red-400'
    case 'partial': return 'text-orange-300'
    default: return 'text-gray-600'
  }
}
