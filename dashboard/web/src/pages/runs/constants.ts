import type { RunKind, RunStatus } from '@/api/hooks'

export const STATUS_OPTIONS: ReadonlyArray<RunStatus | ''> = [
  '',
  'pending',
  'running',
  'done',
  'failed',
  'interrupted',
]

export const STAGE_KINDS: ReadonlyArray<RunKind> = [
  'baseline',
  'current',
  'comparator',
  'report',
]

export const FETCH_LIMIT = 500
