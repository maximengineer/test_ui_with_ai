import { useQuery } from '@tanstack/react-query'

import { api } from '../client'

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

export function reportScreenshotUrl(
  date: string,
  runId: string,
  urlId: string,
  which: 'baseline' | 'current' | 'diff',
): string {
  const params = new URLSearchParams({ url_id: urlId, which })
  return `/api/reports/${encodeURIComponent(date)}/${encodeURIComponent(runId)}/screenshot?${params}`
}

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
