/**
 * Runs page: session-grouped list of runs + spawn buttons + detail drawer.
 *
 * Each row in the table is a SESSION, not a single backend `runs` row -
 * one cycle of (baseline, current, comparator, report) for a date.
 * Sessions are inferred client-side from positional pairing within each
 * date_dir; see `lib/sessions.ts` for the grouping rule.
 *
 * Per-stage pills act as both status indicator and action target:
 *   - Filled pill -> opens that backend row's detail panel
 *   - Empty pill  -> spawns that stage for this session (back-end enforces
 *                    the upstream-completeness precondition; surfaces 412
 *                    if e.g. you click "Run report" before comparator is done)
 *
 * Default view collapses to the LATEST session per date so a normal day
 * looks like one row. The per-date "Show N prior" expander reveals older
 * sessions for that date inline (e.g. when the operator re-snapshotted).
 */
import { useMemo, useState } from 'react'

import {
  useBulkDeleteRuns,
  useRunAll,
  useRuns,
  useSpawnRun,
  type RunAllState,
  type RunKind,
  type RunRow,
  type RunStatus,
} from '@/api/hooks'
import { StatusPill } from '@/components/StatusPill'
import { RunDetailPanel } from '@/components/RunDetailPanel'
import {
  groupRunsIntoSessions,
  sessionMatchesStatus,
  sessionsPerDate,
  type Session,
} from '@/lib/sessions'

const STATUS_OPTIONS: ReadonlyArray<RunStatus | ''> = [
  '',
  'pending',
  'running',
  'done',
  'failed',
  'interrupted',
]

const STAGE_KINDS: ReadonlyArray<RunKind> = [
  'baseline',
  'current',
  'comparator',
  'report',
]

// The page fetches a generous slice of recent rows and groups client-side.
// 500 is the API's max per page; ~4 stages * a typical day gets us ~125
// days of history before we'd need real pagination. Plenty for v1.
const FETCH_LIMIT = 500

export function RunsPage() {
  const [status, setStatus] = useState<RunStatus | ''>('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  // (no expanded-dates state - all sessions are visible at all times)
  // Bulk-select state. Set-of-numbers (db_ids) so a session-level
  // checkbox selecting 4 rows shares storage with the underlying
  // bulk-delete machinery.
  const [checkedIds, setCheckedIds] = useState<Set<number>>(new Set())

  // No `kind` filter at the API level - sessions need ALL kinds to pair
  // up. Status filter is applied client-side at the SESSION level (any
  // stage match).
  const runs = useRuns({ limit: FETCH_LIMIT })
  const spawn = useSpawnRun()
  const bulkDelete = useBulkDeleteRuns()
  const runAll = useRunAll()

  // Group into sessions, then optionally collapse to latest-per-date and
  // filter by status. Memoized on `runs.data` (stable across re-renders
  // when payload hasn't changed) - NOT on a freshly-allocated `items`
  // fallback, which would defeat the memo.
  const allSessions = useMemo(
    () => groupRunsIntoSessions(runs.data?.items ?? []),
    [runs.data],
  )
  const perDateCounts = useMemo(
    () => sessionsPerDate(allSessions),
    [allSessions],
  )
  // Visible sessions = all sessions (no latest-per-date collapse).
  // groupRunsIntoSessions already returns them sorted (date DESC,
  // sequence DESC within a date), so the natural order is "newest
  // first" without any further work. Status filter applied at the
  // session level (any-stage match).
  const visibleSessions = useMemo(() => {
    if (!status) return allSessions
    return allSessions.filter((s) => sessionMatchesStatus(s, status))
  }, [allSessions, status])

  function toggleSession(session: Session, checked: boolean) {
    setCheckedIds((prev) => {
      const next = new Set(prev)
      for (const id of session.dbIds) {
        if (checked) next.add(id)
        else next.delete(id)
      }
      return next
    })
  }

  function clearSelection() {
    setCheckedIds(new Set())
  }

  function spawnStage(kind: RunKind, session: Session) {
    // For comparator+report we wire in the session's own input ids so the
    // backend pairs THIS session's baseline+current (not "latest"), which
    // matters when there are multiple sessions for the same date.
    if (kind === 'comparator') {
      spawn.mutate({
        kind: 'comparator',
        baseline_run_id: session.stages.baseline?.run_id,
        current_run_id: session.stages.current?.run_id,
      })
    } else if (kind === 'report') {
      spawn.mutate({ kind: 'report', date: session.date })
    } else {
      spawn.mutate({ kind })
    }
  }

  // Sessions that are fully selected (all their dbIds checked). The
  // session checkbox is all-or-nothing, so partially-selected only happens
  // if checkedIds got out of sync (defensive); we still count those as
  // selected for display purposes.
  const selectedSessions = useMemo(
    () =>
      visibleSessions.filter(
        (s) => s.dbIds.length > 0 && s.dbIds.every((id) => checkedIds.has(id)),
      ),
    [visibleSessions, checkedIds],
  )
  const selectedSessionCount = selectedSessions.length

  // Cascade rule for re-run: re-running stage X invalidates everything
  // downstream because each stage's output depends on the prior stage's.
  // (Re-run baseline → snapshot of "what the site looked like" changes →
  //  the existing current is from a different point in time → the
  //  comparator's diff is meaningless → the report's analysis is moot.)
  const DOWNSTREAM_KINDS: Record<RunKind, RunKind[]> = {
    baseline: ['current', 'comparator', 'report'],
    current: ['comparator', 'report'],
    comparator: ['report'],
    report: [],
  }

  function rerunStageForSelected(kind: RunKind) {
    if (selectedSessions.length === 0) return
    const cascade = [kind, ...DOWNSTREAM_KINDS[kind]]
    const sessionWord = selectedSessions.length === 1 ? 'session' : 'sessions'
    const cascadeText =
      cascade.length === 1
        ? `the existing ${kind}`
        : `the existing ${cascade.join(' + ')}`

    if (
      !window.confirm(
        `Re-run ${kind} for ${selectedSessions.length} ${sessionWord}?\n\n` +
          `This deletes ${cascadeText} row(s) and on-disk artifacts ` +
          `for each selected session, then spawns a fresh ${kind} run ` +
          `that re-uses the session's preserved upstream inputs.\n\n` +
          `Backend caps concurrent subprocesses at AFR_DASHBOARD_MAX_` +
          `CONCURRENT_RUNS (default 2); extras queue as pending.\n\n` +
          `Cannot be undone. Pending/running rows will be skipped (you ` +
          `must wait for them to terminate first).`,
      )
    ) {
      return
    }

    // 1. Collect db_ids to delete for the cascade. We snapshot the
    //    selectedSessions list NOW because the bulk-delete mutation
    //    will invalidate the runs query, which will re-derive
    //    visibleSessions, which will reset selectedSessions.
    const sessionsSnapshot = [...selectedSessions]
    const toDelete: number[] = []
    for (const session of sessionsSnapshot) {
      for (const k of cascade) {
        const row = session.stages[k]
        if (row) toDelete.push(row.id)
      }
    }

    // 2. Bulk-delete (best-effort; per-id outcomes returned). After
    //    delete completes, fan out N spawns (one per session). The
    //    backend's semaphore (AFR_DASHBOARD_MAX_CONCURRENT_RUNS=2)
    //    naturally serializes them so we don't need a chain here.
    const fireSpawns = () => {
      for (const session of sessionsSnapshot) {
        spawnStage(kind, session)
      }
      clearSelection()
    }

    if (toDelete.length === 0) {
      // Nothing to delete (the selected sessions don't have this stage
      // yet); just spawn. Same path as a fresh per-stage click but
      // wired through the session args.
      fireSpawns()
      return
    }

    bulkDelete.mutate(toDelete, {
      onSuccess: () => {
        // Close the detail panel if it was open on a row we just deleted.
        if (selectedId !== null && toDelete.includes(selectedId)) {
          setSelectedId(null)
        }
        fireSpawns()
      },
    })
  }

  // Top-bar per-stage button click. Three modes:
  //   1. No selection: spawn fresh (legacy behavior). Only enabled
  //      for `baseline` because spawning a fresh current/comparator/
  //      report with no session context creates orphan rows that
  //      land as their own positionally-paired session #N with all
  //      other stages empty (the original "all dashes" bug).
  //   2. With selection AND any selected session has stage X:
  //      re-run mode (cascade-delete stage X + downstream, spawn
  //      fresh stage X tied to each session's preserved upstream).
  //   3. With selection AND no selected session has stage X yet:
  //      forward-spawn for the selected session(s) - no delete
  //      needed (nothing to delete), the new row pairs positionally
  //      with the selected session's existing upstream rows.
  function handleStageButtonClick(kind: RunKind) {
    if (selectedSessions.length === 0) {
      // Mode 1. Only baseline reaches here; the others are disabled.
      spawn.mutate({ kind })
      return
    }
    // Modes 2 + 3 share the same machinery - rerunStageForSelected
    // does cascade delete only on rows that actually exist, then
    // spawns one new stage X per selected session with that session's
    // args. Operationally it does the right thing for both cases.
    rerunStageForSelected(kind)
  }

  // Whether the per-stage button represents a destructive re-run
  // (amber + confirm dialog) vs. a forward spawn (slate). Re-run mode
  // is triggered when ANY selected session already has stage X - the
  // cascade will delete those instances.
  function isRerunForKind(kind: RunKind): boolean {
    return selectedSessions.some((s) => s.stages[kind] !== undefined)
  }

  // Disable rule:
  //   - With selection: nothing disabled (always at least one
  //     legitimate action - re-run if exists, run if missing).
  //   - Without selection: only baseline is enabled (others would
  //     create orphan sessions).
  function isStageButtonDisabled(kind: RunKind): boolean {
    if (selectedSessions.length > 0) return false
    return kind !== 'baseline'
  }

  function confirmAndDelete() {
    const ids = Array.from(checkedIds)
    if (ids.length === 0) return
    // Talk about sessions in the headline (matches the operator's mental
    // model - they checked N session boxes); mention the underlying row
    // count parenthetically so they know the actual blast radius.
    const sessionWord = selectedSessionCount === 1 ? 'session' : 'sessions'
    const rowWord = ids.length === 1 ? 'row' : 'rows'
    if (
      !window.confirm(
        `Delete ${selectedSessionCount} ${sessionWord}?\n\n` +
          `This will remove ${ids.length} underlying run ${rowWord} ` +
          `(baseline + current + comparator + report rows for each ` +
          `session), their on-disk artifact directories, and their ` +
          `log files. Cannot be undone. Rows that are still pending ` +
          'or running will be skipped.',
      )
    ) {
      return
    }
    bulkDelete.mutate(ids, {
      onSuccess: (result) => {
        if (
          selectedId !== null &&
          (result.deleted ?? []).includes(selectedId)
        ) {
          setSelectedId(null)
        }
        clearSelection()
      },
    })
  }

  return (
    <div className="flex h-full">
      <div className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-4">
          <div>
            <h1 className="text-xl font-semibold">Runs</h1>
            <p className="text-sm text-slate-500">
              {runs.isLoading
                ? 'Loading…'
                : `${allSessions.length} session${
                    allSessions.length === 1 ? '' : 's'
                  } across ${perDateCounts.size} date${
                    perDateCounts.size === 1 ? '' : 's'
                  }`}
            </p>
          </div>
          <div className="flex items-center gap-2">
            {/* Run all sits first + has its own color so the operator's
                eye lands on the one-click flow. The per-kind buttons
                stay for partial / debugging runs. */}
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
            {/* Per-stage buttons are context-aware:
                - 0 sessions checked: spawn fresh (slate-900, "Run X")
                - 1+ checked: re-run mode (amber, "Re-run X (N)") -
                  cascades-delete stage X + downstream for each
                  selected session, then spawns a new stage X tied to
                  each session's preserved upstream args.
                Pre-fix, "Run comparator" with no selection always
                spawned a fresh comparator that landed as a brand-new
                session #N with no positional baseline/current pair -
                the operator-confusing "all dashes" symptom. */}
            {STAGE_KINDS.map((k) => (
              <SpawnButton
                key={k}
                kind={k}
                spawn={spawn}
                selectedCount={selectedSessions.length}
                isRerun={isRerunForKind(k)}
                disabled={isStageButtonDisabled(k)}
                onClick={() => handleStageButtonClick(k)}
                busy={bulkDelete.isPending}
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
              setStatus(v as RunStatus | '')
              setSelectedId(null)
              clearSelection()
            }}
          />
          {status && (
            <button
              className="text-xs text-slate-500 underline-offset-2 hover:underline"
              onClick={() => {
                setStatus('')
                clearSelection()
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
          <SpawnErrorBanner error={spawn.error} onDismiss={spawn.reset} />
        )}

        {runAll.state.phase !== 'idle' && (
          <RunAllBanner
            state={runAll.state}
            onCancel={runAll.cancel}
            onDismiss={runAll.reset}
            onJumpTo={(dbId) => setSelectedId(dbId)}
          />
        )}

        {checkedIds.size > 0 && (
          <div className="flex items-center justify-between border-b border-slate-200 bg-blue-50 px-6 py-2 text-sm">
            {/* Talk in SESSIONS (matches what the operator just clicked
                on); show the underlying row count as a subtle aside so
                they know the blast radius is N×4 backend rows. */}
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
                onClick={clearSelection}
                className="rounded border border-slate-300 bg-white px-3 py-1 text-xs hover:bg-slate-100"
              >
                Clear
              </button>
              <button
                onClick={confirmAndDelete}
                disabled={bulkDelete.isPending}
                className="rounded bg-red-600 px-3 py-1 text-xs font-medium text-white disabled:opacity-50 hover:bg-red-700"
              >
                {bulkDelete.isPending
                  ? 'Deleting…'
                  : `Delete ${selectedSessionCount} session${
                      selectedSessionCount === 1 ? '' : 's'
                    }`}
              </button>
            </div>
          </div>
        )}

        {bulkDelete.isSuccess && bulkDelete.data && (
          <BulkDeleteResultBanner
            result={bulkDelete.data}
            onDismiss={() => bulkDelete.reset()}
          />
        )}
        {bulkDelete.isError && (
          <div className="flex items-center justify-between border-b border-red-200 bg-red-50 px-6 py-2 text-sm text-red-800">
            <span>
              Bulk delete failed: {(bulkDelete.error as Error).message}
            </span>
            <button
              onClick={() => bulkDelete.reset()}
              className="rounded px-2 py-1 text-xs hover:bg-red-100"
            >
              Dismiss
            </button>
          </div>
        )}

        <div className="flex-1 overflow-auto">
          {runs.isError ? (
            <div className="p-6 text-sm text-red-700">
              Failed to load runs: {(runs.error as Error).message}
            </div>
          ) : visibleSessions.length === 0 && !runs.isLoading ? (
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
                      onToggle={toggleSession}
                      onSelectRow={setSelectedId}
                      onSpawnStage={spawnStage}
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
          onClose={() => setSelectedId(null)}
        />
      )}
    </div>
  )
}

// --------------------------------------------------------------------------
// Sub-components - kept inline because they're page-private and small.
// --------------------------------------------------------------------------

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
  // Session is "fully checked" when every backend row it owns is in the
  // checked set. Drives the row's checkbox state.
  const allChecked =
    session.dbIds.length > 0 &&
    session.dbIds.every((id) => checkedIds.has(id))

  const sessionContainsSelected =
    selectedId !== null && session.dbIds.includes(selectedId)

  // Date label: if the date has only one session, just show the date;
  // otherwise show "DATE #N" so the operator can disambiguate. All
  // sessions are always visible (no collapse), so #N appears on every
  // session whenever the date has more than one cycle.
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
  // Empty stage -> "Run X" link button. Backend enforces upstream
  // preconditions (412) so we let the operator try and surface any
  // failure via the existing SpawnErrorBanner; no client-side gating.
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
  // Filled stage -> clickable status pill + run_id tail. Click anywhere
  // in the cell opens that row's detail panel.
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
  spawn: ReturnType<typeof useSpawnRun>
  selectedCount: number
  isRerun: boolean
  disabled: boolean
  onClick: () => void
  busy: boolean
}) {
  // Three visual modes:
  //   1. No selection, baseline button: slate, "Run baseline" - spawns
  //      a fresh new session.
  //   2. No selection, current/comparator/report: DISABLED (greyed).
  //      Spawning these without a session context creates orphan rows
  //      that show up as their own session #N with all other stages
  //      empty (the original "all dashes" symptom). The disabled
  //      state has a tooltip pointing the operator at the checkbox.
  //   3. With selection AND any selected session has stage X: amber,
  //      "Re-run X (N)" - cascade deletes stage X + downstream for
  //      each selected session, then spawns fresh X tied to the
  //      session's preserved upstream args. Amber signals destructive.
  //   4. With selection AND no session has stage X yet: slate,
  //      "Run X (N)" - just spawns stage X for each selected session
  //      with that session's args (no delete, nothing to cascade).
  //      Same color as mode #1 because both are non-destructive
  //      "spawn fresh" operations.
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
  // While running: blue progress strip with the active stage + Cancel.
  // Terminal states use the same color vocabulary as the rest of the
  // dashboard (green = ok, red = failure, amber = aborted).
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

  // canceled
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
  // The OpenAPI generator marks these fields optional (Pydantic gives them
  // a default_factory), so normalize to empty arrays for arithmetic.
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
