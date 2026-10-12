import type { ReactNode } from 'react'
import { act, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, type InfiniteData } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Job, JobList } from '../api/types'
import { useCoverLetter, useFeedback } from './useJobs'

const post = vi.fn()
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: { post: (path: string, data?: unknown) => post(path, data) },
}))

const JOB = { id: 5, title: 'Backend Intern', feedback: 'interested' } as Job

function setup() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  client.setQueryData<InfiniteData<JobList>>(['jobs', 'feed', {}], {
    pages: [{ items: [JOB], total: 1, page: 1, per_page: 25 }],
    pageParams: [1],
  })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  const feedbackOf = () =>
    client.getQueryData<InfiniteData<JobList>>(['jobs', 'feed', {}])?.pages[0].items[0].feedback
  return { client, wrapper, feedbackOf }
}

describe('useJobs mutations', () => {
  beforeEach(() => {
    post.mockReset()
  })

  it('rolls failed feedback back to what it was, not to none', async () => {
    post.mockRejectedValue(new Error('offline'))
    const { wrapper, feedbackOf } = setup()
    const { result } = renderHook(() => useFeedback(), { wrapper })

    act(() => result.current.mutate({ jobId: JOB.id, feedback: 'not_relevant' }))
    expect(feedbackOf()).toBe('not_relevant')
    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(feedbackOf()).toBe('interested')
  })

  it('refreshes the CV page letter list after generating one', async () => {
    post.mockResolvedValue({ content: 'Dear Acme', cached: false })
    const { client, wrapper } = setup()
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const { result } = renderHook(() => useCoverLetter(), { wrapper })

    act(() => result.current.mutate(JOB.id))
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['cover-letters'] })
  })
})
