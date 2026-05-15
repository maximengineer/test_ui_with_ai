import type { SpawnRunInput, RunKind } from '@/api/hooks'
import type { Session } from '@/lib/sessions'

const DOWNSTREAM_KINDS: Record<RunKind, RunKind[]> = {
  baseline: ['current', 'comparator', 'report'],
  current: ['comparator', 'report'],
  comparator: ['report'],
  report: [],
}

export function cascadeKinds(kind: RunKind): RunKind[] {
  return [kind, ...DOWNSTREAM_KINDS[kind]]
}

export function buildCascadeDeleteIds(
  sessions: Session[],
  kindsToDelete: RunKind[],
): number[] {
  const toDelete: number[] = []
  for (const session of sessions) {
    for (const k of kindsToDelete) {
      const row = session.stages[k]
      if (row) toDelete.push(row.id)
    }
  }
  return toDelete
}

export function spawnStageForSession(
  spawn: (input: SpawnRunInput) => void,
  kind: RunKind,
  session: Session,
) {
  if (kind === 'comparator') {
    spawn({
      kind: 'comparator',
      baseline_run_id: session.stages.baseline?.run_id,
      current_run_id: session.stages.current?.run_id,
    })
  } else if (kind === 'report') {
    spawn({ kind: 'report', date: session.date })
  } else {
    spawn({ kind })
  }
}

export function isRerunForKind(
  kind: RunKind,
  selectedSessions: Session[],
): boolean {
  return selectedSessions.some((s) => s.stages[kind] !== undefined)
}

export function isStageButtonDisabled(
  kind: RunKind,
  selectedCount: number,
): boolean {
  if (selectedCount > 0) return false
  return kind !== 'baseline'
}
