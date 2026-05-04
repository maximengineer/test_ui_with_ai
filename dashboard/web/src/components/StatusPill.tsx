/**
 * Color-coded status pill used in the runs table + detail header.
 *
 * Centralized here so a future status (e.g. `paused`) gets ONE place
 * to add a color, not one per page.
 */
import type { RunStatus } from '@/api/hooks'

const STATUS_STYLES: Record<RunStatus, string> = {
  pending: 'bg-slate-200 text-slate-700',
  running: 'bg-blue-100 text-blue-800',
  done: 'bg-green-100 text-green-800',
  failed: 'bg-red-100 text-red-800',
  interrupted: 'bg-amber-100 text-amber-800',
}

export function StatusPill({ status }: { status: RunStatus }) {
  const cls = STATUS_STYLES[status] ?? 'bg-slate-100 text-slate-700'
  return (
    <span
      className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {status}
    </span>
  )
}
