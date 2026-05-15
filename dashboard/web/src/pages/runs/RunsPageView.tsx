import type {
  RunAllState,
  RunKind,
  RunRow,
  RunStatus,
  SpawnRunMutation,
} from '@/api/hooks'
import { StatusPill } from '@/components/StatusPill'
import { RunDetailPanel } from '@/components/RunDetailPanel'
import type { Session } from '@/lib/sessions'

import { STAGE_KINDS, STATUS_OPTIONS } from './constants'

export function RunsPageView({
  allSessions,
  perDateCount,
  visibleSessions,
  status,
  onStatusChange,
  selectedId,
  onSelectId,
  checkedIds,
  selectedSessionCount,
  onClearSelection,
  onConfirmDelete,
  runsIsLoading,
  runsErrorMessage,
  spawn,
  runAll,
  onRunAllCancel,
  onRunAllDismiss,
  onBulkDeleteDismiss,
  bulkDeleteResult,
  bulkDeleteError,
  bulkDeletePending,
  onDismissSpawnError,
  onSpawnStageButtonClick,
  isRerunForKind,
  isStageButtonDisabled,
  perDateCounts,
  onToggleSession,
  onSpawnStage,
}: {
  allSessions: Session[]
  perDateCount: number
  visibleSessions: Session[]
  status: RunStatus | ''
  onStatusChange: (v: RunStatus | '') => void
  selectedId: number | null
  onSelectId: (id: number | null) => void
  checkedIds: Set<number>
  selectedSessionCount: number
  onClearSelection: () => void
  onConfirmDelete: () => void
  runsIsLoading: boolean
  runsErrorMessage: string | null
  spawn: SpawnRunMutation
  runAll: { state: RunAllState; start: () => void }
  onRunAllCancel: () => void
  onRunAllDismiss: () => void
  onBulkDeleteDismiss: () => void
  bulkDeleteResult:
    | {
        deleted?: number[]
        skipped_not_found?: number[]
        skipped_in_flight?: number[]
      }
    | null
  bulkDeleteError: string | null
  bulkDeletePending: boolean
  onDismissSpawnError: () => void
  onSpawnStageButtonClick: (kind: RunKind) => void
  isRerunForKind: (kind: RunKind) => boolean
  isStageButtonDisabled: (kind: RunKind) => boolean
  perDateCounts: Map<string, number>
  onToggleSession: (session: Session, checked: boolean) => void
  onSpawnStage: (kind: RunKind, session: Session) => void
}) {
  return (
    <div className="flex h-full">
      <div className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-4">
          <div>
            <h1 className="text-xl font-semibold">Runs</h1>
            <p className="text-sm text-slate-500">
              {runsIsLoading
                ? 'Loading…'
                : `${allSessions.length} session${
                    allSessions.length === 1 ? '' : 's'
                  } across ${perDateCount} date${
                    perDateCount === 1 ? '' : 's'
                  }`}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={runAll.start}
              disabled={runAll.state.phase === 'running'}
              className="rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white disabled:cursor-not-allowed disabled:opacity-50 hover:bg-blue-700"
              title="Run baseline → current → comparator → report sequentially"
            >
              {runAll.state.phase === 'running'
                ? `Running ${runAll.state.currentStep}/${runAll.state.totalSteps}…`
                : 'Run all'}
            </button>
            <span className="mx-1 h-6 w-px bg-slate-200" aria-hidden />
            {STAGE_KINDS.map((k) => (
              <SpawnButton
                key={k}
                kind={k}
                spawn={spawn}
                selectedCount={selectedSessionCount}
                isRerun={isRerunForKind(k)}
                disabled={isStageButtonDisabled(k)}
                onClick={() => onSpawnStageButtonClick(k)}
                busy={bulkDeletePending}
              />
            ))}
          </div>
        </header>

        <div className="flex items-center gap-3 border-b border-slate-200 bg-white px-6 py-2 text-sm">
          <FilterSelect
            label="Status"
            value={status}
            options={STATUS_OPTIONS}
            onChange={(v) => {
              onStatusChange(v as RunStatus | '')
              onSelectId(null)
              onClearSelection()
            }}
          />
          {status && (
            <button
              className="text-xs text-slate-500 underline-offset-2 hover:underline"
              onClick={() => {
                onStatusChange('')
                onClearSelection()
              }}
            >
              Clear filter
            </button>
          )}
          <span className="ml-auto text-xs text-slate-500">
            One row per session • click a stage pill to open its detail
          </span>
        </div>

        {spawn.isError && (
          <SpawnErrorBanner error={spawn.error} onDismiss={onDismissSpawnError} />
        )}

        {runAll.state.phase !== 'idle' && (
          <RunAllBanner
            state={runAll.state}
            onCancel={onRunAllCancel}
            onDismiss={onRunAllDismiss}
            onJumpTo={(dbId) => onSelectId(dbId)}
          />
        )}

        {checkedIds.size > 0 && (
          <div className="flex items-center justify-between border-b border-slate-200 bg-blue-50 px-6 py-2 text-sm">
            <span className="text-slate-700">
              {selectedSessionCount} session
              {selectedSessionCount === 1 ? '' : 's'} selected
              <span className="ml-2 text-xs text-slate-500">
                ({checkedIds.size} underlying run row
                {checkedIds.size === 1 ? '' : 's'})
              </span>
            </span>
            <div className="flex gap-2">
              <button
                onClick={onClearSelection}
                className="rounded border border-slate-300 bg-white px-3 py-1 text-xs hover:bg-slate-100"
              >
                Clear
              </button>
              <button
                onClick={onConfirmDelete}
                disabled={bulkDeletePending}
                className="rounded bg-red-600 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 hover:bg-red-700"
              >
                {bulkDeletePending
                  ? 'Deleting…'
                  : `Delete ${selectedSessionCount} session${
                      selectedSessionCount === 1 ? '' : 's'
                    }`}
              </button>
            </div>
          </div>
        )}

        {bulkDeleteResult && (
          <BulkDeleteResultBanner
            result={bulkDeleteResult}
            onDismiss={onBulkDeleteDismiss}
          />
        )}
        {bulkDeleteError && (
          <div className="flex items-center justify-between border-b border-red-200 bg-red-50 px-6 py-2 text-sm text-red-800">
            <span>Bulk delete failed: {bulkDeleteError}</span>
            <button
              onClick={onBulkDeleteDismiss}
              className="rounded px-2 py-1 text-xs hover:bg-red-100"
            >
              Dismiss
            </button>
          </div>
        )}

        <div className="flex-1 overflow-auto">
          {runsErrorMessage ? (
            <div className="p-6 text-sm text-red-700">
              Failed to load runs: {runsErrorMessage}
            </div>
          ) : visibleSessions.length === 0 && !runsIsLoading ? (
            <div className="p-6 text-sm text-slate-500">
              No sessions yet. Click <strong>Run all</strong> to start one.
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="sticky top-0 z-10 border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="w-[1%] px-4 py-2 text-left" />
                  <th className="px-4 py-2 text-left">Session</th>
                  {STAGE_KINDS.map((k) => (
                    <th key={k} className="px-4 py-2 text-left">
                      {k}
                    </th>
                  ))}
                  <th className="px-4 py-2 text-left">Latest activity</th>
                </tr>
              </thead>
              <tbody>
                {visibleSessions.map((session) => {
                  const totalForDate = perDateCounts.get(session.date) ?? 1
                  return (
                    <SessionRow
                      key={`${session.date}#${session.sequence}`}
                      session={session}
                      totalForDate={totalForDate}
                      checkedIds={checkedIds}
                      selectedId={selectedId}
                      spawnPending={spawn.isPending}
                      onToggle={onToggleSession}
                      onSelectRow={onSelectId}
                      onSpawnStage={onSpawnStage}
                    />
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {selectedId !== null && (
        <RunDetailPanel
          dbId={selectedId}
          onClose={() => onSelectId(null)}
        />
      )}
    </div>
  )
}

function SessionRow({
  session,
  totalForDate,
  checkedIds,
  selectedId,
  spawnPending,
  onToggle,
  onSelectRow,
  onSpawnStage,
}: {
  session: Session
  totalForDate: number
  checkedIds: Set<number>
  selectedId: number | null
  spawnPending: boolean
  onToggle: (s: Session, checked: boolean) => void
  onSelectRow: (id: number) => void
  onSpawnStage: (kind: RunKind, s: Session) => void
}) {
  const allChecked =
    session.dbIds.length > 0 &&
    session.dbIds.every((id) => checkedIds.has(id))

  const sessionContainsSelected =
    selectedId !== null && session.dbIds.includes(selectedId)

  const showSequence = totalForDate > 1
  const dateLabel = showSequence
    ? `${session.date} #${session.sequence}`
    : session.date

  return (
    <tr
      className={`border-b border-slate-100 hover:bg-slate-50 ${
        sessionContainsSelected ? 'bg-blue-50' : ''
      }`}
    >
      <td className="w-[1%] px-4 py-2 align-top">
        <input
          type="checkbox"
          checked={allChecked}
          onChange={(e) => onToggle(session, e.target.checked)}
          aria-label={`Select session ${dateLabel}`}
          className="cursor-pointer"
        />
      </td>
      <td className="px-4 py-2 align-top">
        <div className="font-medium text-slate-900">{dateLabel}</div>
      </td>
      {STAGE_KINDS.map((kind) => (
        <td key={kind} className="px-4 py-2 align-top">
          <StageCell
            kind={kind}
            row={session.stages[kind]}
            isSelected={
              session.stages[kind] !== undefined &&
              session.stages[kind]!.id === selectedId
            }
            spawnPending={spawnPending}
            onOpenDetail={onSelectRow}
            onSpawn={() => onSpawnStage(kind, session)}
          />
        </td>
      ))}
      <td className="px-4 py-2 align-top text-xs text-slate-500">
        {session.latestActivity || '-'}
      </td>
    </tr>
  )
}

function StageCell({
  kind,
  row,
  isSelected,
  spawnPending,
  onOpenDetail,
  onSpawn,
}: {
  kind: RunKind
  row: RunRow | undefined
  isSelected: boolean
  spawnPending: boolean
  onOpenDetail: (id: number) => void
  onSpawn: () => void
}) {
  if (!row) {
    return (
      <button
        onClick={onSpawn}
        disabled={spawnPending}
        className="text-xs text-slate-500 underline-offset-2 hover:text-blue-700 hover:underline disabled:opacity-50"
        title={`Spawn a ${kind} run for this session`}
      >
        — Run {kind}
      </button>
    )
  }
  return (
    <button
      onClick={() => onOpenDetail(row.id)}
      className={`flex flex-col items-start gap-0.5 rounded px-1 py-0.5 text-left hover:bg-blue-100 ${
        isSelected ? 'bg-blue-100' : ''
      }`}
      title={`Open ${kind} detail (db_id ${row.id}, run_id ${row.run_id})`}
    >
      <StatusPill status={row.status} />
      <span className="font-mono text-[10px] text-slate-400">
        {row.run_id.slice(-8)}
      </span>
    </button>
  )
}

function SpawnButton({
  kind,
  spawn,
  selectedCount,
  isRerun,
  disabled,
  onClick,
  busy,
}: {
  kind: RunKind
  spawn: SpawnRunMutation
  selectedCount: number
  isRerun: boolean
  disabled: boolean
  onClick: () => void
  busy: boolean
}) {
  const colors = isRerun
    ? 'bg-amber-600 hover:bg-amber-700'
    : 'bg-slate-900 hover:bg-slate-700'
  const label =
    selectedCount === 0
      ? `Run ${kind}`
      : isRerun
        ? `Re-run ${kind} (${selectedCount})`
        : `Run ${kind} (${selectedCount})`
  const title = disabled
    ? `Select a session first. Spawning a ${kind} without a session ` +
      `context creates an orphan row that lands in its own session ` +
      `with all other stages empty.`
    : isRerun
      ? `Re-run ${kind} for ${selectedCount} selected session(s). ` +
        `Deletes existing ${kind} and downstream rows + artifacts, ` +
        `then spawns a fresh ${kind} that re-uses each session's ` +
        `preserved upstream inputs.`
      : selectedCount === 0
        ? `Spawn a fresh ${kind} run (creates a new session)`
        : `Run ${kind} for ${selectedCount} selected session(s) ` +
          `(no existing ${kind} to delete; just spawns fresh).`
  return (
    <button
      onClick={onClick}
      disabled={disabled || spawn.isPending || busy}
      className={`rounded-md ${colors} px-3 py-1.5 text-xs font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-300 disabled:opacity-60`}
      title={title}
    >
      {label}
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

function SpawnErrorBanner({
  error,
  onDismiss,
}: {
  error: unknown
  onDismiss: () => void
}) {
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

function RunAllBanner({
  state,
  onCancel,
  onDismiss,
  onJumpTo,
}: {
  state: RunAllState
  onCancel: () => void
  onDismiss: () => void
  onJumpTo: (dbId: number) => void
}) {
  if (state.phase === 'running') {
    const kindLabel = state.currentKind ?? '…'
    return (
      <div className="flex items-center justify-between border-b border-blue-200 bg-blue-50 px-6 py-2 text-sm text-blue-900">
        <span>
          Running stage {state.currentStep}/{state.totalSteps}:{' '}
          <strong>{kindLabel}</strong>…
        </span>
        <button
          onClick={onCancel}
          className="rounded border border-blue-300 bg-white px-3 py-1 text-xs hover:bg-blue-100"
        >
          Cancel
        </button>
      </div>
    )
  }

  if (state.phase === 'success') {
    return (
      <div className="flex items-center justify-between border-b border-green-200 bg-green-50 px-6 py-2 text-sm text-green-900">
        <span>All {state.totalSteps} stages complete.</span>
        <button
          onClick={onDismiss}
          className="rounded px-2 py-1 text-xs hover:bg-white"
        >
          Dismiss
        </button>
      </div>
    )
  }

  if (state.phase === 'failed') {
    const failedDbId = state.failedKind
      ? state.spawnedDbIds[state.failedKind]
      : undefined
    return (
      <div className="flex items-center justify-between border-b border-red-200 bg-red-50 px-6 py-2 text-sm text-red-800">
        <span>
          Stage {state.currentStep} ({state.failedKind ?? '?'}) failed:{' '}
          {state.error ?? 'unknown error'}.
        </span>
        <div className="flex items-center gap-2">
          {typeof failedDbId === 'number' && (
            <button
              onClick={() => onJumpTo(failedDbId)}
              className="rounded border border-red-300 bg-white px-2 py-1 text-xs hover:bg-red-100"
            >
              Open row
            </button>
          )}
          <button
            onClick={onDismiss}
            className="rounded px-2 py-1 text-xs hover:bg-red-100"
          >
            Dismiss
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-between border-b border-amber-200 bg-amber-50 px-6 py-2 text-sm text-amber-900">
      <span>
        Run all canceled at stage {state.currentStep} ({state.currentKind ?? '?'}).
        The in-flight subprocess on the server keeps running.
      </span>
      <button
        onClick={onDismiss}
        className="rounded px-2 py-1 text-xs hover:bg-white"
      >
        Dismiss
      </button>
    </div>
  )
}

function BulkDeleteResultBanner({
  result,
  onDismiss,
}: {
  result: {
    deleted?: number[]
    skipped_not_found?: number[]
    skipped_in_flight?: number[]
  }
  onDismiss: () => void
}) {
  const deleted = result.deleted ?? []
  const skippedNotFound = result.skipped_not_found ?? []
  const skippedInFlight = result.skipped_in_flight ?? []

  const skippedTotal = skippedNotFound.length + skippedInFlight.length
  const skippedFragments: string[] = []
  if (skippedNotFound.length > 0) {
    skippedFragments.push(`${skippedNotFound.length} not found`)
  }
  if (skippedInFlight.length > 0) {
    skippedFragments.push(
      `${skippedInFlight.length} still running (cannot delete in-flight)`,
    )
  }
  const message = skippedTotal
    ? `Deleted ${deleted.length}. Skipped ${skippedTotal} (${skippedFragments.join(', ')}).`
    : `Deleted ${deleted.length} run${deleted.length === 1 ? '' : 's'}.`

  const tone = skippedTotal
    ? 'border-amber-200 bg-amber-50 text-amber-900'
    : 'border-green-200 bg-green-50 text-green-900'

  return (
    <div
      className={`flex items-center justify-between border-b ${tone} px-6 py-2 text-sm`}
    >
      <span>{message}</span>
      <button
        onClick={onDismiss}
        className="rounded px-2 py-1 text-xs hover:bg-white"
      >
        Dismiss
      </button>
    </div>
  )
}
