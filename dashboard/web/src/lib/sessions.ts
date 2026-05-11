/**
 * Group flat `RunRow[]` into "sessions" - one per (date, cycle).
 *
 * A session collects the four stage rows (baseline / current / comparator /
 * report) that belong to one comparison cycle. The data model has no
 * `session_id` column - we infer cycles by chronological position within a
 * date: the Nth baseline of the day pairs with the Nth current, the Nth
 * comparator, and the Nth report.
 *
 * Edge cases:
 *   - Stages can be missing (e.g. baseline+current done but comparator not
 *     started yet) - the corresponding slot is undefined.
 *   - A date with 2 baselines and 1 current produces 2 sessions; the second
 *     has only the baseline filled.
 *   - Rows whose `date_dir` is null are skipped (those are pre-B.1 rows
 *     that don't belong to any date bucket).
 *
 * Sequence numbering: 1-indexed, oldest cycle of the day = #1.
 */
import type { RunKind, RunRow } from '@/api/hooks'

export type SessionStages = {
  baseline?: RunRow
  current?: RunRow
  comparator?: RunRow
  report?: RunRow
}

export type Session = {
  date: string // DD-MM-YYYY
  sequence: number // 1-indexed within the date; #1 = oldest
  stages: SessionStages
  // db_ids of all stages present (for bulk-delete + checkbox selection)
  dbIds: number[]
  // ISO datetime of the most recent activity in this session - drives the
  // "newest first" sort and the date-group ordering.
  latestActivity: string
}

const STAGE_KINDS: ReadonlyArray<RunKind> = [
  'baseline',
  'current',
  'comparator',
  'report',
]

export function groupRunsIntoSessions(runs: RunRow[]): Session[] {
  // Bucket by date_dir, then by kind within each date.
  const byDate = new Map<string, Map<RunKind, RunRow[]>>()
  for (const row of runs) {
    if (!row.date_dir) continue
    let kindMap = byDate.get(row.date_dir)
    if (!kindMap) {
      kindMap = new Map()
      byDate.set(row.date_dir, kindMap)
    }
    const arr = kindMap.get(row.kind) ?? []
    arr.push(row)
    kindMap.set(row.kind, arr)
  }

  const sessions: Session[] = []
  for (const [date, kindMap] of byDate.entries()) {
    // Sort each kind's rows oldest-first so positional pairing yields
    // session #1 = first cycle of the day. Tie-break by id (db rowid is
    // monotonic) when created_at strings collide.
    for (const kind of STAGE_KINDS) {
      const arr = kindMap.get(kind)
      if (arr) {
        arr.sort((a, b) => {
          if (a.created_at !== b.created_at) {
            return a.created_at < b.created_at ? -1 : 1
          }
          return a.id - b.id
        })
      }
    }

    const maxLen = Math.max(
      ...STAGE_KINDS.map((k) => kindMap.get(k)?.length ?? 0),
    )
    for (let i = 0; i < maxLen; i++) {
      const stages: SessionStages = {}
      const dbIds: number[] = []
      let latestActivity = ''
      for (const kind of STAGE_KINDS) {
        const row = kindMap.get(kind)?.[i]
        if (row) {
          stages[kind] = row
          dbIds.push(row.id)
          // Track the most recent activity timestamp - prefer finished_at,
          // then started_at, then created_at. Lexicographic compare works
          // because the API returns ISO-8601 strings.
          const ts = row.finished_at ?? row.started_at ?? row.created_at
          if (ts > latestActivity) latestActivity = ts
        }
      }
      sessions.push({
        date,
        sequence: i + 1,
        stages,
        dbIds,
        latestActivity,
      })
    }
  }

  // Display order: most-recent date first, and within a date the latest
  // session (highest sequence) first. Falls back to latestActivity when
  // dates collide on the string compare (they shouldn't, but defensive).
  sessions.sort((a, b) => {
    if (a.date !== b.date) return a.date < b.date ? 1 : -1
    return b.sequence - a.sequence
  })
  return sessions
}

/**
 * Keep only the latest (highest-sequence) session per date. Used by the
 * default "collapsed" view; the operator opts into prior sessions via the
 * inline expander.
 */
export function latestPerDate(sessions: Session[]): Session[] {
  const seen = new Set<string>()
  const out: Session[] = []
  for (const s of sessions) {
    if (seen.has(s.date)) continue
    seen.add(s.date)
    out.push(s)
  }
  return out
}

/**
 * Count of sessions per date - used by the row's "Show N prior" expander.
 */
export function sessionsPerDate(sessions: Session[]): Map<string, number> {
  const counts = new Map<string, number>()
  for (const s of sessions) {
    counts.set(s.date, (counts.get(s.date) ?? 0) + 1)
  }
  return counts
}

/**
 * True if any stage in the session has the given status. Drives the
 * session-level Status filter.
 */
export function sessionMatchesStatus(
  session: Session,
  status: RunRow['status'],
): boolean {
  for (const kind of STAGE_KINDS) {
    if (session.stages[kind]?.status === status) return true
  }
  return false
}
