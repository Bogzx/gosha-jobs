import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../api/client'
import type { Me } from '../api/types'
import { forgetConsent } from '../lib/cvConsent'

/**
 * The signed-in user, or null when logged out. Cached app-wide.
 *
 * Only a 401 means logged out. Anything else (a 5xx or a dropped
 * connection while the API restarts) is `isError`, so callers can offer a
 * retry instead of bouncing a signed-in user to the landing page.
 */
export function useMe() {
  const query = useQuery<Me | null>({
    queryKey: ['me'],
    queryFn: async () => {
      try {
        return await api.get<Me>('/me')
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) return null
        throw err
      }
    },
    staleTime: 5 * 60_000,
    // Ride out a restart (5xx / network); other 4xx won't fix themselves.
    retry: (failureCount, err) =>
      failureCount < 2 && !(err instanceof ApiError && err.status < 500),
  })
  return {
    me: query.data ?? null,
    isLoading: query.isLoading,
    isError: query.isError,
    isFetching: query.isFetching,
    refetch: query.refetch,
  }
}

export function useLogout() {
  const queryClient = useQueryClient()
  return async () => {
    forgetConsent(queryClient.getQueryData<Me | null>(['me'])?.id)
    await api.post('/auth/logout')
    queryClient.setQueryData(['me'], null)
    queryClient.clear()
    window.location.href = '/'
  }
}
