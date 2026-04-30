// Map agentloop status → Tailwind text color. Centralized so header bar,
// recent sidebar, and workspace drawer all stay in sync.
export function agentloopStatusColor(status) {
  switch (status) {
    case 'running': return 'text-yellow-400'
    case 'done': return 'text-green-500'
    case 'exhausted': return 'text-orange-400'
    case 'stopped': return 'text-gray-500'
    default: return 'text-gray-600'
  }
}
