import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Job, JobList, Me } from '../api/types'
import Feed from './Feed'

function job(id: number, title: string): Job {
  return {
    id, url: `https://example.com/${id}`, title, company: 'Acme', location: 'Cluj',
    description: `${title} description`, salary_min: null, salary_max: null,
    salary_currency: null, salary_period: null, salary_monthly_min_ron: null,
    salary_monthly_max_ron: null, source: 'ejobs', posted_at: null,
    first_seen_at: new Date().toISOString(), match_score: null,
    match_percentile: null, match_reasons: null, match_signals: null,
    feedback: null, applied: false,
  }
}

const ME = { id: 7, has_cv: true } as Me
const JOBS = [job(1, 'Python Developer'), job(2, 'Data Engineer'), job(3, 'QA Intern')]

// Page 2 repeats job 2: a new posting arrived between the two requests
// and pushed it across the page boundary.
const PAGES: Record<number, JobList> = {
  1: { items: [JOBS[0], JOBS[1]], total: 4, page: 1, per_page: 2 },
  2: { items: [JOBS[1], JOBS[2]], total: 4, page: 2, per_page: 2 },
}

const get = vi.fn()
const post = vi.fn()
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: {
    get: (path: string) => get(path),
    post: (path: string, data?: unknown) => post(path, data),
  },
}))

// A resizable viewport: matchMedia that reports `desktop` and notifies
// subscribers when it changes.
let desktop = true
const mediaListeners = new Set<() => void>()
const realMatchMedia = window.matchMedia

function resize(toDesktop: boolean) {
  desktop = toDesktop
  act(() => mediaListeners.forEach((notify) => notify()))
}

function renderFeed() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Feed />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const card = (title: string) => screen.getByRole('button', { name: new RegExp(title) })
const detailHeading = (title: string) =>
  screen.findByRole('heading', { level: 2, name: title })

describe('Feed', () => {
  beforeEach(() => {
    desktop = true
    mediaListeners.clear()
    window.matchMedia = (query: string) =>
      ({
        media: query,
        get matches() {
          return desktop
        },
        addEventListener: (_type: string, notify: () => void) => {
          mediaListeners.add(notify)
        },
        removeEventListener: (_type: string, notify: () => void) => {
          mediaListeners.delete(notify)
        },
      }) as unknown as MediaQueryList

    get.mockReset()
    post.mockReset()
    get.mockImplementation(async (path: string) => {
      if (path === '/me') return ME
      const [route, qs] = path.split('?')
      if (route === '/feed' || route === '/jobs') {
        return PAGES[Number(new URLSearchParams(qs).get('page'))]
      }
      const detail = JOBS.find((j) => route === `/jobs/${j.id}`)
      if (detail) return detail
      throw new Error(`unexpected GET ${path}`)
    })
  })
  afterEach(() => {
    window.matchMedia = realMatchMedia
  })

  it('drops the cover letter when another job is selected', async () => {
    post.mockImplementation(async (path: string) => ({
      content: `Dear Acme, re ${path}`,
      cached: false,
    }))
    renderFeed()
    await detailHeading('Python Developer')

    await userEvent.click(screen.getByRole('button', { name: 'Cover letter' }))
    expect(await screen.findByText('Dear Acme, re /jobs/1/cover-letter')).toBeInTheDocument()

    await userEvent.click(card('Data Engineer'))
    await detailHeading('Data Engineer')
    expect(screen.queryByText(/Dear Acme/)).not.toBeInTheDocument()
  })

  it('does not land a letter still generating on the next job', async () => {
    let finish: (value: { content: string; cached: boolean }) => void = () => undefined
    post.mockImplementation(() => new Promise((resolve) => (finish = resolve)))
    renderFeed()
    await detailHeading('Python Developer')

    await userEvent.click(screen.getByRole('button', { name: 'Cover letter' }))
    await userEvent.click(card('Data Engineer'))
    await detailHeading('Data Engineer')
    expect(screen.getByRole('button', { name: 'Cover letter' })).toBeEnabled()

    await act(async () => finish({ content: 'Dear Acme, for job 1', cached: false }))
    expect(screen.queryByText('Dear Acme, for job 1')).not.toBeInTheDocument()
  })

  it('lets the job sheet be closed after the window narrows', async () => {
    renderFeed()
    await detailHeading('Python Developer')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    // Desktop's "first job" fallback must not become a phone sheet.
    resize(false)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    await userEvent.click(card('Data Engineer'))
    const sheet = screen.getByRole('dialog', { name: 'Job details' })
    expect(within(sheet).getByRole('heading', { level: 2 })).toHaveTextContent('Data Engineer')
    await userEvent.click(within(sheet).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    await userEvent.click(card('Python Developer'))
    expect(screen.getByRole('dialog', { name: 'Job details' })).toBeInTheDocument()
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('shows a job once when it shifts onto the next page', async () => {
    const consoleError = vi.spyOn(console, 'error')
    renderFeed()
    const list = await screen.findByRole('region', { name: 'Job list' })
    await within(list).findByText('Python Developer')

    await userEvent.click(screen.getByRole('button', { name: 'Load more' }))
    await within(list).findByText('QA Intern')
    expect(within(list).getAllByRole('listitem')).toHaveLength(3)
    expect(within(list).getAllByText('Data Engineer')).toHaveLength(1)
    const keyWarnings = consoleError.mock.calls.filter(([msg]) => String(msg).includes('same key'))
    expect(keyWarnings).toEqual([])
    consoleError.mockRestore()
  })

  describe('scope', () => {
    // Scoped by default: only what the active searches take. scope=all
    // returns everything.
    function serveFeed(scoped: JobList, all: JobList) {
      const fallback = get.getMockImplementation()!
      get.mockImplementation(async (path: string) => {
        const [route, qs] = path.split('?')
        if (route !== '/feed') return fallback(path)
        return new URLSearchParams(qs).get('scope') === 'all' ? all : scoped
      })
    }
    const page = (items: Job[], extra: Partial<JobList>): JobList => ({
      items, total: items.length, page: 1, per_page: 25, ...extra,
    })

    it('says the feed is narrowed and toggles to everything and back', async () => {
      serveFeed(
        page([JOBS[0]], { scoped: true, hidden_by_scope: 2 }),
        page(JOBS, { scoped: false, hidden_by_scope: 0 }),
      )
      renderFeed()
      expect(await screen.findByText(/Matching your active searches/)).toBeInTheDocument()
      expect(card('Python Developer')).toBeInTheDocument()

      await userEvent.click(screen.getByRole('button', { name: 'Show all' }))
      expect(get).toHaveBeenCalledWith('/feed?page=1&per_page=25&scope=all')
      expect(await screen.findByText(/Showing every posting/)).toBeInTheDocument()
      await screen.findByRole('button', { name: /QA Intern/ })

      await userEvent.click(screen.getByRole('button', { name: 'Only my searches' }))
      expect(await screen.findByText(/Matching your active searches/)).toBeInTheDocument()
    })

    it('explains an empty scoped feed and offers the rest', async () => {
      serveFeed(
        page([], { scoped: true, hidden_by_scope: 3 }),
        page(JOBS, { scoped: false, hidden_by_scope: 0 }),
      )
      renderFeed()
      expect(
        await screen.findByText(/3 postings are outside your active searches' locations and levels/),
      ).toBeInTheDocument()
      expect(screen.queryByText('Nothing here yet')).not.toBeInTheDocument()

      // Both the line above the list and the empty state offer it.
      const [first] = screen.getAllByRole('button', { name: 'Show all' })
      await userEvent.click(first)
      await screen.findByRole('button', { name: /QA Intern/ })
    })

    it('says nothing about scope when no search narrowed the feed', async () => {
      serveFeed(page(JOBS, { scoped: false, hidden_by_scope: 0 }), page(JOBS, {}))
      renderFeed()
      await screen.findByRole('button', { name: /QA Intern/ })
      expect(screen.queryByText(/Matching your active searches/)).not.toBeInTheDocument()
    })
  })

  it('searches once typing pauses, keeping the list up meanwhile', async () => {
    renderFeed()
    await screen.findByRole('button', { name: /Python Developer/ })

    await userEvent.type(screen.getByLabelText('Search'), 'qa')
    expect(card('Python Developer')).toBeInTheDocument()

    await waitFor(() =>
      expect(get).toHaveBeenCalledWith('/jobs?page=1&per_page=25&q=qa'),
    )
    const browseCalls = get.mock.calls
      .map(([path]) => path as string)
      .filter((path) => path.startsWith('/jobs?'))
    expect(browseCalls).toEqual(['/jobs?page=1&per_page=25&q=qa'])
  })
})
