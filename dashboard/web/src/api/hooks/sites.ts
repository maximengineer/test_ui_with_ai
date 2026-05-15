import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '../client'

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

export function useBulkCreateSites() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (urls: string[]) => {
      const { data, error, response } = await api.POST('/api/sites/bulk', {
        body: { urls },
      })
      if (error) {
        throw Object.assign(
          new Error(`bulk create failed: HTTP ${response.status}`),
          { status: response.status, detail: error },
        )
      }
      return data ?? []
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

export function useBulkDeleteSites() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (ids: string[]) => {
      const { data, error, response } = await api.POST(
        '/api/sites/bulk-delete',
        { body: { ids } },
      )
      if (error) {
        throw Object.assign(
          new Error(`bulk delete failed: HTTP ${response.status}`),
          { status: response.status, detail: error },
        )
      }
      return data!
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sites'] }),
  })
}
