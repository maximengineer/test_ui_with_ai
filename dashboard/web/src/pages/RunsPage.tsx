/**
 * Runs page: list of runs + spawn buttons + detail drawer.
 *
 * Layout choice: master-detail in a single page rather than a separate
 * route, because the detail (PID, log tail, retry button) is small enough
 * to slide in alongside the list and the operator wants to switch
 * between rows quickly. Selecting a row highlights it in the table and
 * pops the detail panel; clicking again or pressing Esc closes it.
 *
 * Filters: kind + status, both optional. Pagination: 50/page (matches
 * the backend default; the API caps at 500 if we ever raise it).
 */
import { useState } from 'react'

import {
  useRuns,
  useSpawnRun,
  type RunKind,
  type RunStatus,
} from '@/api/hooks'
import { StatusPill } from '@/components/StatusPill'
import { RunDetailPanel } from '@/components/RunDetailPanel'

const KIND_OPTIONS: ReadonlyArray<RunKind | ''> = [
  '',
  'baseline',
  'current',
  'comparator',
  'report',
]
const STATUS_OPTIONS: ReadonlyArray<RunStatus | ''> = [
  '',
  'pending',
  'running',
  'done',
  'failed',
  'interrupted',
]
const PAGE_SIZE = 50

export function RunsPage() {
  const [kind, setKind] = useState<RunKind | ''>('')
  const [status, setStatus] = useState<RunStatus | ''>('')
  const [offset, setOffset] = useState(0)
  const [selectedId, setSelectedId] = useState<number | null>(null)

  const runs = useRuns({
    kind: kind || undefined,
    status: status || undefined,
    limit: PAGE_SIZE,
    offset,
  })
  const spawn = useSpawnRun()

  const items = runs.data?.items ?? []
  const total = runs.data?.total ?? 0

  return (
    <div className="flex h-full">
      <div className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-4">
          <div>
            <h1 className="text-xl font-semibold">Runs</h1>
            <p className="text-sm text-slate-500">
              {runs.isLoading ? 'Loading…' : `${total} total`}
            </p>
          </div>
          <div className="flex gap-2">
            <SpawnButton kind="baseline" spawn={spawn} />
            <SpawnButton kind="current" spawn={spawn} />
            <SpawnButton kind="comparator" spawn={spawn} />
            <SpawnButton kind="report" spawn={spawn} />
          </div>
        </header>

        <div className="flex items-center gap-3 border-b border-slate-200 bg-white px-6 py-2 text-sm">
          <FilterSelect
            label="Kind"
            value={kind}
            options={KIND_OPTIONS}
            onChange={(v) => {
              setKind(v as RunKind | '')
              setOffset(0)
              setSelectedId(null)
            }}
          />
          <FilterSelect
            label="Status"
            value={status}
            options={STATUS_OPTIONS}
            onChange={(v) => {
              setStatus(v as RunStatus | '')
              setOffset(0)
              setSelectedId(null)
            }}
          />
          {(kind || status) && (
            <button
              className="text-xs text-slate-500 underline-offset-2 hover:underline"
              onClick={() => {
                setKind('')
                setStatus('')
                setOffset(0)
              }}
            >
              Clear filters
            </button>
          )}
        </div>

        {spawn.isError && (
          <SpawnErrorBanner error={spawn.error} onDismiss={spawn.reset} />
        )}

        <div className="flex-1 overflow-auto">
          {runs.isError ? (
            <div className="p-6 text-sm text-red-700">
              Failed to load runs: {(runs.error as Error).message}
            </div>
          ) : items.length === 0 && !runs.isLoading ? (
            <div className="p-6 text-sm text-slate-500">
              No runs match these filters.
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="sticky top-0 border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-4 py-2 text-left">ID</th>
                  <th className="px-4 py-2 text-left">Kind</th>
                  <th className="px-4 py-2 text-left">Status</th>
                  <th className="px-4 py-2 text-left">Date</th>
                  <th className="px-4 py-2 text-left">Started</th>
                  <th className="px-4 py-2 text-left">Run ID</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr
                    key={row.id}
                    className={`cursor-pointer border-b border-slate-100 hover:bg-slate-50 ${
                      selectedId === row.id ? 'bg-blue-50' : ''
                    }`}
                    onClick={() => setSelectedId(row.id)}
                  >
                    <td className="px-4 py-2 font-mono text-xs">{row.id}</td>
                    <td className="px-4 py-2">{row.kind}</td>
                    <td className="px-4 py-2">
                      <StatusPill status={row.status} />
                    </td>
                    <td className="px-4 py-2 text-slate-600">{row.date_dir ?? '-'}</td>
                    <td className="px-4 py-2 text-slate-600">
                      {row.started_at ?? '-'}
                    </td>
                    <td className="px-4 py-2 font-mono text-xs text-slate-500">
                      {row.run_id}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <Pagination
          offset={offset}
          pageSize={PAGE_SIZE}
          total={total}
          onPrev={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          onNext={() => setOffset(offset + PAGE_SIZE)}
        />
      </div>

      {selectedId !== null && (
        <RunDetailPanel
          dbId={selectedId}
          onClose={() => setSelectedId(null)}
        />
      )}
    </div>
  )
}

// --------------------------------------------------------------------------
// Sub-components - kept inline because they're page-private and small.
// --------------------------------------------------------------------------

function SpawnButton({
  kind,
  spawn,
}: {
  kind: RunKind
  spawn: ReturnType<typeof useSpawnRun>
}) {
  return (
    <button
      onClick={() => spawn.mutate({ kind })}
      disabled={spawn.isPending}
      className="rounded-md bg-slate-900 px-3 py-1.5 text-xs font-medium text-white disabled:cursor-not-allowed disabled:opacity-50 hover:bg-slate-700"
    >
      Run {kind}
    </button>
  )
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: ReadonlyArray<string>
  onChange: (v: string) => void
}) {
  return (
    <label className="flex items-center gap-2 text-slate-600">
      {label}:
      <select
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((opt) => (
          <option key={opt} value={opt}>
            {opt || '(any)'}
          </option>
        ))}
      </select>
    </label>
  )
}

function Pagination({
  offset,
  pageSize,
  total,
  onPrev,
  onNext,
}: {
  offset: number
  pageSize: number
  total: number
  onPrev: () => void
  onNext: () => void
}) {
  const showingFrom = total === 0 ? 0 : offset + 1
  const showingTo = Math.min(offset + pageSize, total)
  const hasPrev = offset > 0
  const hasNext = offset + pageSize < total

  return (
    <div className="flex items-center justify-between border-t border-slate-200 bg-white px-6 py-2 text-xs text-slate-600">
      <span>
        {showingFrom}–{showingTo} of {total}
      </span>
      <div className="flex gap-2">
        <button
          onClick={onPrev}
          disabled={!hasPrev}
          className="rounded border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Prev
        </button>
        <button
          onClick={onNext}
          disabled={!hasNext}
          className="rounded border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next
        </button>
      </div>
    </div>
  )
}

function SpawnErrorBanner({
  error,
  onDismiss,
}: {
  error: unknown
  onDismiss: () => void
}) {
  // The hook attaches `status` and `detail` to the thrown Error so we can
  // tailor the message: 409 = "already running", 412 = precondition.
  const e = error as { status?: number; message: string; detail?: unknown }
  const message =
    e.status === 409
      ? 'A run for this kind+date is already in flight.'
      : e.status === 412
      ? `Workflow precondition failed: ${
          typeof e.detail === 'string' ? e.detail : 'upstream not complete.'
        }`
      : e.message

  return (
    <div className="flex items-center justify-between border-b border-red-200 bg-red-50 px-6 py-2 text-sm text-red-800">
      <span>{message}</span>
      <button
        onClick={onDismiss}
        className="rounded px-2 py-1 text-xs hover:bg-red-100"
      >
        Dismiss
      </button>
    </div>
  )
}
