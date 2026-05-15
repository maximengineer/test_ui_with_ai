import { useCallback, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../client'
import type { RunKind, RunStatus } from './types'

export interface RunsListParams {
  kind?: RunKind
  status?: RunStatus
  limit?: number
  offset?: number
}

export function useRuns(params: RunsListParams = {}) {
  return useQuery({
    queryKey: ['runs', params],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/runs', {
        params: { query: params },
      })
      if (error) throw error
      return data
    },
    refetchInterval: 3000,
  })
}

const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set([
  'done',
  'failed',
  'interrupted',
])

export function useRun(dbId: number | null) {
  return useQuery({
    queryKey: ['run', dbId],
    enabled: dbId !== null,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/runs/{db_id}', {
        params: { path: { db_id: dbId! } },
      })
      if (error) throw error
      return data
    },
    refetchInterval: (query) => {
      const row = query.state.data
      if (!row) return 2000
      return TERMINAL_STATUSES.has(row.status) ? false : 2000
    },
  })
}

export function useRunLogs(
  dbId: number | null,
  status: RunStatus | null,
  tail = 32_768,
) {
  return useQuery({
    queryKey: ['runLogs', dbId, tail],
    enabled: dbId !== null,
    queryFn: async () => {
      const resp = await fetch(`/api/runs/${dbId}/logs?tail=${tail}`)
      if (!resp.ok) {
        if (resp.status === 404) return ''
        throw new Error(`logs failed: HTTP ${resp.status}`)
      }
      return resp.text()
    },
    refetchInterval: status && TERMINAL_STATUSES.has(status) ? false : 3000,
  })
}

export type SpawnRunInput =
  | { kind: 'baseline' }
  | { kind: 'current' }
  | { kind: 'comparator'; baseline_run_id?: string; current_run_id?: string }
  | { kind: 'report'; date?: string }

export function useSpawnRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (input: SpawnRunInput) => {
      const { data, error, response } = await (() => {
        switch (input.kind) {
          case 'baseline':
            return api.POST('/api/runs', { body: { kind: 'baseline' } })
          case 'current':
            return api.POST('/api/runs', { body: { kind: 'current' } })
          case 'comparator':
            return api.POST('/api/runs', {
              body: {
                kind: 'comparator',
                baseline_run_id: input.baseline_run_id,
                current_run_id: input.current_run_id,
              },
            })
          case 'report':
            return api.POST('/api/runs', {
              body: { kind: 'report', date: input.date },
            })
          default: {
            const _exhaustive: never = input
            throw new Error(
              `useSpawnRun: unhandled RunKind ${JSON.stringify(_exhaustive)}`,
            )
          }
        }
      })()
      if (error) {
        throw Object.assign(new Error(`spawn failed: HTTP ${response.status}`), {
          status: response.status,
          detail: error,
        })
      }
      return { input, result: data }
    },
    onSuccess: ({ input }) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['dates'] })
      if (input.kind === 'report') {
        qc.invalidateQueries({ queryKey: ['report'] })
      }
    },
  })
}

export type SpawnRunMutation = ReturnType<typeof useSpawnRun>

export function useRetryRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (dbId: number) => {
      const { data, error, response } = await api.POST(
        '/api/runs/{db_id}/retry',
        { params: { path: { db_id: dbId } } },
      )
      if (error) {
        throw Object.assign(
          new Error(`retry failed: HTTP ${response.status}`),
          { status: response.status, detail: error },
        )
      }
      return data
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs'] })
    },
  })
}

export function useDeleteRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (dbId: number) => {
      const { error, response } = await api.DELETE('/api/runs/{db_id}', {
        params: { path: { db_id: dbId } },
      })
      if (error) {
        throw Object.assign(
          new Error(`delete failed: HTTP ${response.status}`),
          { status: response.status, detail: error },
        )
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['dates'] })
    },
  })
}

export function useBulkDeleteRuns() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (dbIds: number[]) => {
      const { data, error, response } = await api.POST(
        '/api/runs/bulk-delete',
        { body: { db_ids: dbIds } },
      )
      if (error) {
        throw Object.assign(
          new Error(`bulk delete failed: HTTP ${response.status}`),
          { status: response.status, detail: error },
        )
      }
      return data!
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['dates'] })
    },
  })
}

const RUN_ALL_SEQUENCE: ReadonlyArray<RunKind> = [
  'baseline',
  'current',
  'comparator',
  'report',
]

const POLL_INTERVAL_MS = 2000

export type RunAllPhase =
  | 'idle'
  | 'running'
  | 'success'
  | 'failed'
  | 'canceled'

export type RunAllState = {
  phase: RunAllPhase
  currentStep: number
  totalSteps: number
  currentKind: RunKind | null
  spawnedDbIds: Partial<Record<RunKind, number>>
  error: string | null
  failedKind: RunKind | null
}

const INITIAL_STATE: RunAllState = {
  phase: 'idle',
  currentStep: 0,
  totalSteps: RUN_ALL_SEQUENCE.length,
  currentKind: null,
  spawnedDbIds: {},
  error: null,
  failedKind: null,
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export function useRunAll() {
  const qc = useQueryClient()
  const [state, setState] = useState<RunAllState>(INITIAL_STATE)
  const canceledRef = useRef(false)

  const spawnOne = useCallback(
    async (kind: RunKind): Promise<number> => {
      const result = await (() => {
        switch (kind) {
          case 'baseline':
            return api.POST('/api/runs', { body: { kind: 'baseline' } })
          case 'current':
            return api.POST('/api/runs', { body: { kind: 'current' } })
          case 'comparator':
            return api.POST('/api/runs', { body: { kind: 'comparator' } })
          case 'report':
            return api.POST('/api/runs', { body: { kind: 'report' } })
          default: {
            const _exhaustive: never = kind
            throw new Error(
              `useRunAll: unhandled RunKind ${JSON.stringify(_exhaustive)}`,
            )
          }
        }
      })()

      if (result.error) {
        const status = result.response.status
        if (status === 409) {
          const detail = result.error as { detail?: { existing_db_id?: number } }
          const existing =
            detail?.detail?.existing_db_id ??
            (result.error as { existing_db_id?: number }).existing_db_id
          if (typeof existing === 'number') return existing
        }
        throw Object.assign(
          new Error(`spawn ${kind} failed: HTTP ${status}`),
          { status, detail: result.error },
        )
      }
      return result.data!.db_id
    },
    [],
  )

  const pollUntilTerminal = useCallback(
    async (dbId: number): Promise<RunStatus> => {
      while (!canceledRef.current) {
        const { data, error } = await api.GET('/api/runs/{db_id}', {
          params: { path: { db_id: dbId } },
        })
        if (error) {
          throw Object.assign(new Error(`poll db_id=${dbId} failed`), {
            detail: error,
          })
        }
        const row = data!
        if (TERMINAL_STATUSES.has(row.status)) return row.status
        await sleep(POLL_INTERVAL_MS)
      }
      return 'interrupted'
    },
    [],
  )

  const start = useCallback(() => {
    if (state.phase === 'running') return
    canceledRef.current = false
    setState({ ...INITIAL_STATE, phase: 'running' })

    void (async () => {
      const spawned: Partial<Record<RunKind, number>> = {}
      for (let i = 0; i < RUN_ALL_SEQUENCE.length; i++) {
        const kind = RUN_ALL_SEQUENCE[i]
        if (canceledRef.current) {
          setState((s) => ({ ...s, phase: 'canceled' }))
          return
        }
        setState((s) => ({
          ...s,
          currentStep: i + 1,
          currentKind: kind,
          spawnedDbIds: { ...spawned },
        }))

        let dbId: number
        try {
          dbId = await spawnOne(kind)
        } catch (e) {
          setState((s) => ({
            ...s,
            phase: 'failed',
            failedKind: kind,
            error: (e as Error).message,
          }))
          qc.invalidateQueries({ queryKey: ['runs'] })
          return
        }
        spawned[kind] = dbId
        setState((s) => ({ ...s, spawnedDbIds: { ...spawned } }))
        qc.invalidateQueries({ queryKey: ['runs'] })

        const terminal = await pollUntilTerminal(dbId)
        if (canceledRef.current) {
          setState((s) => ({
            ...s,
            phase: 'canceled',
            spawnedDbIds: { ...spawned },
          }))
          return
        }
        if (terminal !== 'done') {
          setState((s) => ({
            ...s,
            phase: 'failed',
            failedKind: kind,
            error: `${kind} ended with status "${terminal}"`,
            spawnedDbIds: { ...spawned },
          }))
          qc.invalidateQueries({ queryKey: ['runs'] })
          return
        }
      }

      setState((s) => ({
        ...s,
        phase: 'success',
        currentKind: null,
        spawnedDbIds: { ...spawned },
      }))
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['dates'] })
      qc.invalidateQueries({ queryKey: ['report'] })
    })()
  }, [state.phase, spawnOne, pollUntilTerminal, qc])

  const cancel = useCallback(() => {
    if (state.phase !== 'running') return
    canceledRef.current = true
  }, [state.phase])

  const reset = useCallback(() => {
    canceledRef.current = false
    setState(INITIAL_STATE)
  }, [])

  return { state, start, cancel, reset }
}
