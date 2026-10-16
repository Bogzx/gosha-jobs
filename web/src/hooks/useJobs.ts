import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from '@tanstack/react-query'
import { api } from '../api/client'
import type { Application, FeedScope, Job, JobFilters, JobList } from '../api/types'

const PER_PAGE = 25

function filtersToParams(filters: JobFilters, page: number): string {
  const params = new URLSearchParams()
  params.set('page', String(page))
  params.set('per_page', String(PER_PAGE))
  if (filters.q) params.set('q', filters.q)
  if (filters.locations) params.set('locations', filters.locations)
  if (filters.experience) params.set('experience', filters.experience)
  if (filters.sources) params.set('sources', filters.sources)
  if (filters.salary_min) params.set('salary_min', String(filters.salary_min))
  if (filters.posted_within_days)
    params.set('posted_within_days', String(filters.posted_within_days))
  if (filters.remote) params.set('remote', 'true')
  return params.toString()
}

/**
 * Infinite list for either the personalized feed or filtered browsing.
 * The previous list stays on screen while new filters load (or are held
 * back with `enabled`), instead of blanking to a spinner.
 */
export function useJobList(
  mode: 'feed' | 'browse',
  filters: JobFilters,
  enabled = true,
  scope: FeedScope = 'searches',
) {
  return useInfiniteQuery({
    queryKey: ['jobs', mode, filters, scope],
    queryFn: ({ pageParam }) => {
      if (mode === 'browse') return api.get<JobList>(`/jobs?${filtersToParams(filters, pageParam)}`)
      const qs = filtersToParams({}, pageParam)
      return api.get<JobList>(`/feed?${qs}${scope === 'all' ? '&scope=all' : ''}`)
    },
    initialPageParam: 1,
    getNextPageParam: (last) =>
      last.page * last.per_page < last.total ? last.page + 1 : undefined,
    placeholderData: keepPreviousData,
    enabled,
  })
}

/** Patch one job across every cached job list (feed + browse pages). */
function patchJobEverywhere(
  queryClient: ReturnType<typeof useQueryClient>,
  jobId: number,
  patch: Partial<Job>,
) {
  queryClient.setQueriesData<InfiniteData<JobList>>(
    { queryKey: ['jobs'] },
    (data) =>
      data && {
        ...data,
        pages: data.pages.map((page) => ({
          ...page,
          items: page.items.map((job) =>
            job.id === jobId ? { ...job, ...patch } : job,
          ),
        })),
      },
  )
}

/** One job as currently cached in any list, for rolling back a patch. */
function findCachedJob(
  queryClient: ReturnType<typeof useQueryClient>,
  jobId: number,
): Job | undefined {
  for (const [, data] of queryClient.getQueriesData<InfiniteData<JobList>>({
    queryKey: ['jobs'],
  })) {
    for (const page of data?.pages ?? []) {
      const job = page.items.find((item) => item.id === jobId)
      if (job) return job
    }
  }
  return undefined
}

/** Full job record (untruncated description) for the detail pane. */
export function useJobDetail(jobId: number | undefined) {
  return useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api.get<Job>(`/jobs/${jobId}`),
    enabled: jobId != null,
    staleTime: 5 * 60_000,
  })
}

export function useFeedback() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ jobId, feedback }: { jobId: number; feedback: 'interested' | 'not_relevant' }) =>
      api.post(`/jobs/${jobId}/feedback`, { feedback }),
    onMutate: ({ jobId, feedback }) => {
      const previous = findCachedJob(queryClient, jobId)?.feedback ?? null
      patchJobEverywhere(queryClient, jobId, { feedback })
      return { previous }
    },
    onError: (_err, { jobId }, context) => {
      patchJobEverywhere(queryClient, jobId, { feedback: context?.previous ?? null })
    },
  })
}

export function useApplyClick() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (jobId: number) =>
      api.post<Application>(`/jobs/${jobId}/apply-click`),
    onSuccess: (_app, jobId) => {
      patchJobEverywhere(queryClient, jobId, { applied: true })
      void queryClient.invalidateQueries({ queryKey: ['applications'] })
    },
  })
}

export function useUndoApply() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ applicationId }: { applicationId: number; jobId: number }) =>
      api.delete(`/applications/${applicationId}`),
    onSuccess: (_data, { jobId }) => {
      patchJobEverywhere(queryClient, jobId, { applied: false })
      void queryClient.invalidateQueries({ queryKey: ['applications'] })
    },
  })
}

export function useCoverLetter() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (jobId: number) =>
      api.post<{ content: string; cached: boolean }>(`/jobs/${jobId}/cover-letter`),
    // The CV page lists every letter generated so far.
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['cover-letters'] })
    },
  })
}
