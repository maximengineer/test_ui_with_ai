/**
 * Right-side detail panel for a selected run.
 *
 * Shows the canonical fields (db_id, run_id, kind, status with pill,
 * timestamps, exit code, error, PID/PGID), a Retry button, and a
 * tail of the subprocess log. Polls /api/runs/{id} every 2s while
 * the run is still running (the hook handles that internally).
 *
 * Keeps log fetching ALWAYS-ON (3s interval) even after the run ends -
 * a `done` run that interests the operator is exactly the case where
 * they want to read what happened.
 */
import { useEffect } from 'react'

import { useRetryRun, useRun, useRunLogs } from '@/api/hooks'
import { StatusPill } from './StatusPill'

export function RunDetailPanel({
  dbId,
  onClose,
}: {
  dbId: number
  onClose: () => void
}) {
  const run = useRun(dbId)
  // Pass `status` so the logs hook stops polling once the run is
  // terminal (the log file is closed + immutable past that point).
  const logs = useRunLogs(dbId, run.data?.status ?? null, 32_768)
  const retry = useRetryRun()

  // Esc to close - quality-of-life detail the operator notices once and
  // then takes for granted. Cleanup removes the listener on unmount so
  // the next panel doesn't get a stale handler.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <aside className="flex w-[28rem] flex-col border-l border-slate-200 bg-white">
      <header className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-500">
            Run #{dbId}
          </div>
          {run.data && (
            <div className="mt-0.5 flex items-center gap-2">
              <span className="font-medium">{run.data.kind}</span>
              <StatusPill status={run.data.status} />
            </div>
          )}
        </div>
        <button
          onClick={onClose}
          className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          aria-label="Close detail panel"
        >
          ✕
        </button>
      </header>

      <div className="flex-1 overflow-auto px-4 py-3 text-sm">
        {run.isLoading ? (
          <div className="text-slate-500">Loading…</div>
        ) : run.isError || !run.data ? (
          <div className="text-red-700">
            Failed to load run: {(run.error as Error)?.message ?? 'unknown'}
          </div>
        ) : (
          <>
            <DetailGrid>
              <DetailRow label="Run ID" value={run.data.run_id} mono />
              <DetailRow label="Date" value={run.data.date_dir ?? '-'} />
              <DetailRow label="Source" value={run.data.source} />
              <DetailRow label="Started" value={run.data.started_at ?? '-'} />
              <DetailRow label="Finished" value={run.data.finished_at ?? '-'} />
              <DetailRow
                label="Exit code"
                value={
                  run.data.exit_code === null ||
                  run.data.exit_code === undefined
                    ? '-'
                    : String(run.data.exit_code)
                }
              />
              <DetailRow
                label="PID / PGID"
                value={
                  run.data.pid !== null && run.data.pid !== undefined
                    ? `${run.data.pid} / ${run.data.pgid}`
                    : '-'
                }
                mono
              />
              {run.data.error && (
                <DetailRow label="Error" value={run.data.error} className="text-red-700" />
              )}
            </DetailGrid>

            {/* Retry. Only meaningful for terminal runs - pending/running
                would 409. We DO show the button always so the operator
                can read the resulting error if they try; cheaper than
                hiding+explaining-why-hidden in a tooltip. */}
            <div className="mt-4">
              <button
                onClick={() => retry.mutate(dbId)}
                disabled={retry.isPending}
                className="rounded-md border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 disabled:cursor-not-allowed disabled:opacity-50 hover:bg-slate-50"
              >
                {retry.isPending ? 'Retrying…' : 'Retry'}
              </button>
              {retry.isError && (
                <span className="ml-2 text-xs text-red-700">
                  {(retry.error as { status?: number }).status === 409
                    ? '409: a run for this kind+date is already in flight.'
                    : (retry.error as Error).message}
                </span>
              )}
            </div>

            <div className="mt-5">
              <div className="mb-1 text-xs uppercase tracking-wide text-slate-500">
                Log (last 32 KB)
              </div>
              <pre className="max-h-64 overflow-auto rounded-md bg-slate-900 p-3 text-[11px] leading-snug text-slate-100">
                {logs.isLoading
                  ? 'Loading…'
                  : logs.data
                  ? logs.data
                  : '(no log output yet)'}
              </pre>
            </div>
          </>
        )}
      </div>
    </aside>
  )
}

function DetailGrid({ children }: { children: React.ReactNode }) {
  return (
    <dl className="grid grid-cols-[7rem_1fr] gap-y-1.5 text-xs">{children}</dl>
  )
}

function DetailRow({
  label,
  value,
  mono = false,
  className = '',
}: {
  label: string
  value: string
  mono?: boolean
  className?: string
}) {
  return (
    <>
      <dt className="text-slate-500">{label}</dt>
      <dd
        className={`break-all ${mono ? 'font-mono text-[11px]' : ''} ${className}`}
      >
        {value}
      </dd>
    </>
  )
}
