import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Job } from '../api/types'
import Demo, { type DemoPersona } from './Demo'

const PERSONAS: DemoPersona[] = [
  { id: 'python-backend', label: 'Python backend student', summary: 'FastAPI', cv: 'Python CV text' },
  { id: 'frontend-ro', label: 'Frontend student', summary: 'React', cv: 'CV în română' },
]

function job(id: number, title: string, percentile: number): Job {
  return {
    id, url: `https://example.com/${id}`, title, company: 'Acme', location: 'Cluj',
    description: `${title} description`, salary_min: null, salary_max: null,
    salary_currency: null, salary_period: null, salary_monthly_min_ron: null,
    salary_monthly_max_ron: null, source: 'ejobs', posted_at: null,
    first_seen_at: new Date().toISOString(), match_score: 0.4,
    match_percentile: percentile, match_reasons: ['python'],
    match_signals: [{ kind: 'skill', text: 'Your CV mentions python' }],
  } as Job
}

const get = vi.fn()
vi.mock('../api/client', () => ({ api: { get: (path: string) => get(path) } }))
vi.mock('../lib/signin', () => ({ signInWithDiscord: vi.fn() }))

function renderDemo() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Demo />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Demo', () => {
  beforeEach(() => {
    get.mockReset()
    get.mockImplementation(async (path: string) => {
      if (path === '/demo/personas') return PERSONAS
      const persona = PERSONAS.find((p) => path.endsWith(`persona=${p.id}`))!
      const items = persona.id === 'python-backend'
        ? [job(1, 'Python Developer', 99), job(2, 'Data Engineer', 80)]
        : [job(3, 'React Developer', 99)]
      return { persona, items, total: 120 }
    })
  })

  it('ranks jobs for the first sample CV without signing in', async () => {
    renderDemo()
    expect(await screen.findByRole('tab', { name: /Python backend student/ })).toHaveAttribute('aria-selected', 'true')
    const list = await screen.findByRole('region', { name: 'Ranked jobs' })
    expect(within(list).getByText('Python Developer')).toBeInTheDocument()
    expect(within(list).getByText(/top 2 of 120 postings/)).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('/demo/feed?persona=python-backend')
  })

  it('switches persona and shows why the top job matched', async () => {
    renderDemo()
    await userEvent.click(await screen.findByRole('tab', { name: /Frontend student/ }))
    expect(await screen.findAllByText('React Developer')).not.toHaveLength(0)
    expect(screen.getByText('Your CV mentions python')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('/demo/feed?persona=frontend-ro')
  })
})
