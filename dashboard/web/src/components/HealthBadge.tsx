/**
 * Sidebar-footer pill that surfaces /api/health state.
 *
 * Three observable conditions, in order of how much the operator cares:
 *   - DB down           → red    (the dashboard itself is broken)
 *   - AI analyzer down  → amber  (degraded; report runs would fail later)
 *   - both up           → green  (healthy)
 */
import { useHealth } from '@/api/hooks'

export function HealthBadge() {
  const { data, isLoading, isError } = useHealth()

  if (isLoading || isError || !data) {
    return (
      <div className="flex items-center gap-2 text-xs text-slate-500">
        <span className="inline-block h-2 w-2 rounded-full bg-slate-300" />
        Checking…
      </div>
    )
  }

  const dbBad = !data.db_ok
  const aiBad = !data.ai_analyzer_ok
  const color = dbBad ? 'bg-red-500' : aiBad ? 'bg-amber-500' : 'bg-green-500'
  const label = dbBad ? 'DB error' : aiBad ? 'Analyzer down' : 'Healthy'

  return (
    <div className="flex items-center gap-2 text-xs text-slate-700">
      <span className={`inline-block h-2 w-2 rounded-full ${color}`} />
      <span className="font-medium">{label}</span>
    </div>
  )
}
