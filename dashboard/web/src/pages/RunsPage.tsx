import { useMemo, useState } from 'react'

import {
  useBulkDeleteRuns,
  useRunAll,
  useRuns,
  useSpawnRun,
  type RunKind,
  type RunStatus,
} from '@/api/hooks'
import {
  groupRunsIntoSessions,
  sessionMatchesStatus,
  sessionsPerDate,
  type Session,
} from '@/lib/sessions'

import {
  buildCascadeDeleteIds,
  cascadeKinds,
  isRerunForKind as isRerunForKindForSessions,
  isStageButtonDisabled as isStageButtonDisabledForSelection,
  spawnStageForSession,
} from './runs/actions'
import { FETCH_LIMIT } from './runs/constants'
import { RunsPageView } from './runs/RunsPageView'

export function RunsPage() {
  const [status, setStatus] = useState<RunStatus | ''>('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [checkedIds, setCheckedIds] = useState<Set<number>>(new Set())

  const runs = useRuns({ limit: FETCH_LIMIT })
  const spawn = useSpawnRun()
  const bulkDelete = useBulkDeleteRuns()
  const runAll = useRunAll()

  const allSessions = useMemo(
    () => groupRunsIntoSessions(runs.data?.items ?? []),
    [runs.data],
  )
  const perDateCounts = useMemo(() => sessionsPerDate(allSessions), [allSessions])
  const visibleSessions = useMemo(() => {
    if (!status) return allSessions
    return allSessions.filter((s) => sessionMatchesStatus(s, status))
  }, [allSessions, status])

  const selectedSessions = useMemo(
    () =>
      visibleSessions.filter(
        (s) => s.dbIds.length > 0 && s.dbIds.every((id) => checkedIds.has(id)),
      ),
    [visibleSessions, checkedIds],
  )
  const selectedSessionCount = selectedSessions.length

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
    spawnStageForSession(spawn.mutate, kind, session)
  }

  function rerunStageForSelected(kind: RunKind) {
    if (selectedSessions.length === 0) return
    const cascade = cascadeKinds(kind)
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

    const sessionsSnapshot = [...selectedSessions]
    const toDelete = buildCascadeDeleteIds(sessionsSnapshot, cascade)

    const fireSpawns = () => {
      for (const session of sessionsSnapshot) {
        spawnStage(kind, session)
      }
      clearSelection()
    }

    if (toDelete.length === 0) {
      fireSpawns()
      return
    }

    bulkDelete.mutate(toDelete, {
      onSuccess: () => {
        if (selectedId !== null && toDelete.includes(selectedId)) {
          setSelectedId(null)
        }
        fireSpawns()
      },
    })
  }

  function handleStageButtonClick(kind: RunKind) {
    if (selectedSessions.length === 0) {
      spawn.mutate({ kind })
      return
    }
    rerunStageForSelected(kind)
  }

  function isRerunForKind(kind: RunKind): boolean {
    return isRerunForKindForSessions(kind, selectedSessions)
  }

  function isStageButtonDisabled(kind: RunKind): boolean {
    return isStageButtonDisabledForSelection(kind, selectedSessions.length)
  }

  function confirmAndDelete() {
    const ids = Array.from(checkedIds)
    if (ids.length === 0) return

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
    <RunsPageView
      allSessions={allSessions}
      perDateCount={perDateCounts.size}
      visibleSessions={visibleSessions}
      status={status}
      onStatusChange={setStatus}
      selectedId={selectedId}
      onSelectId={setSelectedId}
      checkedIds={checkedIds}
      selectedSessionCount={selectedSessionCount}
      onClearSelection={clearSelection}
      onConfirmDelete={confirmAndDelete}
      runsIsLoading={runs.isLoading}
      runsErrorMessage={runs.isError ? (runs.error as Error).message : null}
      spawn={spawn}
      runAll={{ state: runAll.state, start: runAll.start }}
      onRunAllCancel={runAll.cancel}
      onRunAllDismiss={runAll.reset}
      onBulkDeleteDismiss={() => bulkDelete.reset()}
      bulkDeleteResult={bulkDelete.isSuccess ? (bulkDelete.data ?? null) : null}
      bulkDeleteError={bulkDelete.isError ? (bulkDelete.error as Error).message : null}
      bulkDeletePending={bulkDelete.isPending}
      onDismissSpawnError={() => spawn.reset()}
      onSpawnStageButtonClick={handleStageButtonClick}
      isRerunForKind={isRerunForKind}
      isStageButtonDisabled={isStageButtonDisabled}
      perDateCounts={perDateCounts}
      onToggleSession={toggleSession}
      onSpawnStage={spawnStage}
    />
  )
}
