import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api/client'
import type { Me } from './api/types'
import App from './App'

const get = vi.fn()
vi.mock('./api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./api/client')>()),
  api: { get: (path: string) => get(path) },
  trackPageview: () => undefined,
}))
vi.mock('./lib/signin', () => ({
  prefetchLogin: () => () => undefined,
  signInWithDiscord: vi.fn(),
}))

function renderAt(path: string) {
  // No backoff, so useMe's restart retries run straight through.
  const client = new QueryClient({ defaultOptions: { queries: { retryDelay: 0 } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('App sign-in gate', () => {
  beforeEach(() => {
    get.mockReset()
  })

  it('offers a retry, not the landing page, when /me fails mid-restart', async () => {
    get.mockRejectedValue(new ApiError(503, 'error', 'Request failed (503)'))
    renderAt('/tracker')
    expect(await screen.findByText("Couldn't load your account")).toBeInTheDocument()
    expect(screen.queryByText(/The job feed that/)).not.toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(3)

    get.mockImplementation(async (path: string) =>
      path === '/me' ? ({ id: 7, has_cv: true } as Me) : { items: [] },
    )
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText(/Your applications, tracked/)).toBeInTheDocument()
  })

  it('sends a signed-out visitor to the landing page', async () => {
    get.mockRejectedValue(new ApiError(401, 'unauthorized', 'Sign in'))
    renderAt('/tracker')
    expect(await screen.findByText(/The job feed that/)).toBeInTheDocument()
  })
})
