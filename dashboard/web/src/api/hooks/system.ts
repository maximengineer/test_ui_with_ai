import { useQuery } from '@tanstack/react-query'

import { api } from '../client'

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
