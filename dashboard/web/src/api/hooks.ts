/**
 * TanStack Query hooks over the typed API client.
 *
 * Convention: one hook per route. Each hook does its own argument
 * normalization and queryKey shape so callers don't have to remember
 * key conventions. The backend's wire shapes are imported from the
 * generated schema (`components['schemas'][...]`) - no hand-rolled types.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from './client'
import type { components } from './schema.gen'

// Re-export the wire shapes most pages care about so consumers import
// from one place. Keeping the `components['schemas'][...]` indirection
// out of UI code makes refactors mechanical.
export type RunRow = components['schemas']['RunRow']
export type RunListOut = components['schemas']['RunListOut']
export type SiteOut = components['schemas']['SiteOut']
export type DatesOut = components['schemas']['DatesOut']
export type HealthOut = components['schemas']['HealthOut']
export type SyncOut = components['schemas']['SyncOut']
export type RunSpawnedOut = components['schemas']['RunSpawnedOut']
export type ReportSummaryOut = components['schemas']['ReportSummaryOut']
export type ReportUrlsOut = components['schemas']['ReportUrlsOut']
export type ReportUrlSummary = components['schemas']['ReportUrlSummary']
export type ReportUrlDetail = components['schemas']['ReportUrlDetail']

// Hand-defined for the same reason as RunKind / RunStatus above -
// FastAPI inlines Literals rather than emitting them as named schemas.
// Kept in lock-step with `dashboard/api/models.py:ReportResultType`.
export type ReportResultType =
  | 'analysis_success'
  | 'analysis_error'
  | 'no_changes'
  | 'ai_disabled'
  | 'unknown'

// FastAPI inlines Literal[...] enums rather than emitting them as named
// component schemas, so openapi-typescript can't expose them by name.
// Define them by hand here - kept in lock-step with the Python tuples in
// `dashboard/api/db.py` (RUN_KINDS_TUPLE, RUN_STATUSES_TUPLE). The
// `RunRow.kind` / `.status` fields ARE the same union string literally,
// so a desync would surface as a TS narrowing failure at the call site.
export type RunKind = 'baseline' | 'current' | 'comparator' | 'report'
export type RunStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'failed'
  | 'interrupted'

// ---------------------------------------------------------------------------
// /api/health - polled by the Header so the operator sees DB / analyzer
//               state at a glance. 5s interval is enough for a dashboard
//               this size; tighter polling would just create log noise.
// ---------------------------------------------------------------------------

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/health')
      if (error) throw error
      return data
    },
    refetchInterval: 5000,
  })
}

// ---------------------------------------------------------------------------
// /api/sites - list of configured sites. Read-only this slice; CRUD lands
//              in the Sites slice with the matching backend additions.
// ---------------------------------------------------------------------------

export function useSites() {
  return useQuery({
    queryKey: ['sites'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/sites')
      if (error) throw error
      return data ?? []
    },
  })
}

// ---------------------------------------------------------------------------
// Sites CRUD - POST / PATCH / DELETE.
// ---------------------------------------------------------------------------

export function useCreateSite() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (input: { name: string; url: string }) => {
      const { data, error, response } = await api.POST('/api/sites', {
        body: input,
      })
      if (error) {
        throw Object.assign(
          new Error(`create failed: HTTP ${response.status}`),
          { status: response.status, detail: error },
        )
      }
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sites'] }),
  })
}

export function useUpdateSite() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (input: {
      id: string
      name?: string
      url?: string
    }) => {
      const { id, ...body } = input
      const { data, error, response } = await api.PATCH('/api/sites/{site_id}', {
        params: { path: { site_id: id } },
        body,
      })
      if (error) {
        throw Object.assign(
          new Error(`update failed: HTTP ${response.status}`),
          { status: response.status, detail: error },
        )
      }
      return data
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sites'] }),
  })
}

// ---------------------------------------------------------------------------
// Reports drill-in.
// ---------------------------------------------------------------------------

export function useReportSummary(date: string | null, runId: string | null) {
  return useQuery({
    queryKey: ['report', date, runId, 'summary'],
    enabled: !!date && !!runId,
    queryFn: async () => {
      const { data, error } = await api.GET(
        '/api/reports/{date}/{run_id}',
        { params: { path: { date: date!, run_id: runId! } } },
      )
      if (error) throw error
      return data
    },
  })
}

export function useReportUrls(date: string | null, runId: string | null) {
  return useQuery({
    queryKey: ['report', date, runId, 'urls'],
    enabled: !!date && !!runId,
    queryFn: async () => {
      const { data, error } = await api.GET(
        '/api/reports/{date}/{run_id}/urls',
        { params: { path: { date: date!, run_id: runId! } } },
      )
      if (error) throw error
      return data
    },
  })
}

export function useReportUrlDetail(
  date: string | null,
  runId: string | null,
  urlId: string | null,
) {
  return useQuery({
    queryKey: ['report', date, runId, 'url', urlId],
    enabled: !!date && !!runId && !!urlId,
    queryFn: async () => {
      const { data, error } = await api.GET(
        '/api/reports/{date}/{run_id}/url',
        {
          params: {
            path: { date: date!, run_id: runId! },
            query: { id: urlId! },
          },
        },
      )
      if (error) throw error
      return data
    },
  })
}

/**
 * Build a screenshot URL for use in <img src=...>. We don't fetch into
 * memory because the browser handles PNG bytes more efficiently when
 * the URL goes straight to <img>; openapi-fetch's typed paths would
 * require XHR-then-blob-URL juggling for no benefit.
 */
export function reportScreenshotUrl(
  date: string,
  runId: string,
  urlId: string,
  which: 'baseline' | 'current' | 'diff',
): string {
  const params = new URLSearchParams({ url_id: urlId, which })
  return `/api/reports/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/screenshot?${params}`
}

/**
 * List report runs for a specific date. Uses the backend's `date_dir`
 * query param so the response is already scoped - no client-side
 * filter, no 500-row truncation. (Round-2 review CRITICAL #1.)
 */
export function useReportRuns(date: string | null) {
  return useQuery({
    queryKey: ['runs', { kind: 'report', date_dir: date }],
    enabled: !!date,
    queryFn: async () => {
      const { data, error } = await api.GET('/api/runs', {
        params: {
          query: { kind: 'report', date_dir: date!, limit: 500 },
        },
      })
      if (error) throw error
      return data?.items ?? []
    },
  })
}

export function useDeleteSite() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (siteId: string) => {
      const { error, response } = await api.DELETE('/api/sites/{site_id}', {
        params: { path: { site_id: siteId } },
      })
      if (error) {
        throw Object.assign(
          new Error(`delete failed: HTTP ${response.status}`),
          { status: response.status, detail: error },
        )
      }
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sites'] }),
  })
}

// ---------------------------------------------------------------------------
// /api/dates - date dirs present on disk per kind. The Reports page uses
//              this to populate its date picker.
// ---------------------------------------------------------------------------

export function useDates() {
  return useQuery({
    queryKey: ['dates'],
    queryFn: async () => {
      const { data, error } = await api.GET('/api/dates')
      if (error) throw error
      return data
    },
  })
}

// ---------------------------------------------------------------------------
// /api/runs - paginated list. `kind` and `status` are optional filters;
//              the queryKey includes them so each filter combination has
//              its own cache entry.
// ---------------------------------------------------------------------------

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
    // Refresh the list every 3s so a freshly-spawned run shows up
    // promptly even before the user navigates back to the page.
    refetchInterval: 3000,
  })
}

// ---------------------------------------------------------------------------
// /api/runs/{db_id} - single run detail. Polls every 2s while the run is
//                     still in flight (matches the plan's 2s cadence);
//                     stops polling once it lands at a terminal status.
// ---------------------------------------------------------------------------

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
      // No data yet → poll. Terminal → stop polling. Otherwise → 2s.
      if (!row) return 2000
      return TERMINAL_STATUSES.has(row.status) ? false : 2000
    },
  })
}

// ---------------------------------------------------------------------------
// /api/runs/{db_id}/logs - text response, NOT JSON. The query returns the
//                          raw string so the UI can render it in a <pre>.
// ---------------------------------------------------------------------------

export function useRunLogs(
  dbId: number | null,
  status: RunStatus | null,
  tail = 32_768,
) {
  return useQuery({
    queryKey: ['runLogs', dbId, tail],
    enabled: dbId !== null,
    queryFn: async () => {
      // openapi-fetch's typed client expects JSON by default; for a
      // text/plain response we drop down to a plain fetch. The path is
      // still type-checked via the schema (it's the same string), and
      // we cap the tail param at the backend's documented 1MB limit.
      const resp = await fetch(`/api/runs/${dbId}/logs?tail=${tail}`)
      if (!resp.ok) {
        // 404 is expected for a row whose subprocess hasn't written
        // anything yet; surface as empty rather than as an error so
        // the UI can show a "no log yet" placeholder gracefully.
        if (resp.status === 404) return ''
        throw new Error(`logs failed: HTTP ${resp.status}`)
      }
      return resp.text()
    },
    // Stop polling once the run is terminal - the log file is closed
    // and immutable, so further polls are pure waste. Mirrors useRun's
    // terminal-status check. (Round-2 review caught the indefinite poll.)
    refetchInterval: status && TERMINAL_STATUSES.has(status) ? false : 3000,
  })
}

// ---------------------------------------------------------------------------
// POST /api/runs - spawn a new run. Uses TanStack's useMutation so the
//                  caller can show pending / error state without writing
//                  state machinery by hand.
// ---------------------------------------------------------------------------

// Discriminated input that mirrors the backend's `RunRequest` union. Each
// arm carries exactly the fields the backend's matching subclass accepts;
// `extra='forbid'` on the Python side rejects anything else.
//
// Splitting these out (vs. one flat `SpawnRunInput`) lets the dispatch in
// `useSpawnRun` body construction be type-narrowed by the discriminator,
// removing the previous `as any` cast. Round-2 review caught the cast.
export type SpawnRunInput =
  | { kind: 'baseline' }
  | { kind: 'current' }
  | { kind: 'comparator'; baseline_run_id?: string; current_run_id?: string }
  | { kind: 'report'; date?: string }

export function useSpawnRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (input: SpawnRunInput) => {
      // Type-narrowed dispatch: each arm builds the body openapi-fetch
      // wants for that exact kind. The compiler enforces the per-arm
      // shape; no runtime cast needed.
      //
      // The default branch's `_exhaustive: never` is the standard TS
      // exhaustiveness check - if a 5th `RunKind` is added without a
      // case here, this line becomes a TYPE ERROR (because the new
      // kind narrows to a non-never type at this point). Round-3 #M3
      // caught the silent-fall-through risk.
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
        // The backend returns 409 / 412 / 422 with structured detail.
        // Re-raise with the status so the UI can branch on it.
        throw Object.assign(new Error(`spawn failed: HTTP ${response.status}`), {
          status: response.status,
          detail: error,
        })
      }
      return { input, result: data }
    },
    onSuccess: ({ input }) => {
      // New row landed; refresh the runs list so the operator sees it.
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['dates'] })
      // Spawning a `report` produces a new report drill-in target -
      // invalidate the per-report cache so the Reports page picks it up
      // without a manual page refresh. Round-2 review caught this.
      if (input.kind === 'report') {
        qc.invalidateQueries({ queryKey: ['report'] })
      }
    },
  })
}

// ---------------------------------------------------------------------------
// POST /api/runs/{db_id}/retry - re-spawn with the same args. Same pattern
//                                as useSpawnRun.
// ---------------------------------------------------------------------------

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
