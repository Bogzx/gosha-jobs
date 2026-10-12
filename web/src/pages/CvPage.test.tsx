import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { CoverLetter, Me } from '../api/types'
import CvPage from './CvPage'

const LETTER: CoverLetter = {
  id: 1, job_id: 9, job_title: 'Backend Intern', company: 'Acme',
  content: 'Dear Acme', created_at: new Date().toISOString(),
}

// Server state: deleting the CV deletes every letter written from it.
let hasCv = true
let letters: CoverLetter[] = []

const get = vi.fn()
const del = vi.fn()
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: {
    get: (path: string) => get(path),
    delete: (path: string) => del(path),
  },
}))

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <CvPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('CvPage', () => {
  beforeEach(() => {
    hasCv = true
    letters = [LETTER]
    get.mockReset()
    get.mockImplementation(async (path: string) => {
      if (path === '/me') return { id: 7, has_cv: hasCv } as Me
      if (path === '/cv') {
        return { has_cv: hasCv, text: hasCv ? 'My CV' : null, uploaded_at: null }
      }
      if (path === '/cover-letters') return { items: letters }
      throw new Error(`unexpected GET ${path}`)
    })
    del.mockReset()
    del.mockImplementation(async () => {
      hasCv = false
      letters = []
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })
  afterEach(() => vi.restoreAllMocks())

  it('stops listing cover letters once the CV is deleted', async () => {
    renderPage()
    expect(await screen.findByText('Backend Intern')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Delete CV' }))
    expect(del).toHaveBeenCalledWith('/cv')
    expect(await screen.findByText(/none yet/)).toBeInTheDocument()
    expect(screen.queryByText('Backend Intern')).not.toBeInTheDocument()
  })

  it('says the CV failed to load instead of offering an upload', async () => {
    get.mockImplementation(async (path: string) => {
      if (path === '/cv') throw new Error('502')
      if (path === '/cover-letters') return { items: [] }
      return { id: 7, has_cv: true } as Me
    })
    renderPage()
    expect(await screen.findByText("Couldn't load your CV")).toBeInTheDocument()
    expect(screen.queryByText(/Drop your CV here/)).not.toBeInTheDocument()
  })
})
