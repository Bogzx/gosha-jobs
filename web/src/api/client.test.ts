import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './client'

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn().mockResolvedValue(
    new Response(body === undefined ? '' : JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  vi.stubGlobal('fetch', fn)
  return fn
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api client', () => {
  it('returns parsed JSON on success', async () => {
    mockFetch(200, { ok: true })
    await expect(api.get('/health')).resolves.toEqual({ ok: true })
  })

  it('sends credentials and JSON bodies on POST', async () => {
    const fn = mockFetch(200, { ok: true })
    await api.post('/events/pageview', { path: '/feed' })
    const [url, init] = fn.mock.calls[0]
    expect(url).toBe('/api/v1/events/pageview')
    expect(init.credentials).toBe('include')
    expect(JSON.parse(init.body)).toEqual({ path: '/feed' })
  })

  it('sends the file and extra form fields on putFile', async () => {
    const fn = mockFetch(200, { has_cv: true })
    const file = new File(['Python'], 'cv.txt', { type: 'text/plain' })
    await api.putFile('/cv', file, { consent: 'true' })
    const [url, init] = fn.mock.calls[0]
    expect(url).toBe('/api/v1/cv')
    expect(init.method).toBe('PUT')
    const form = init.body as FormData
    expect((form.get('file') as File).name).toBe('cv.txt')
    expect(form.get('consent')).toBe('true')
  })

  it('parses the error envelope into ApiError', async () => {
    mockFetch(403, { error: { code: 'tier_limit', message: 'Plan limit hit.' } })
    const err = (await api.get('/subscriptions').catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe('tier_limit')
    expect(err.status).toBe(403)
    expect(err.message).toBe('Plan limit hit.')
  })

  it('handles non-JSON error bodies', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('<html>bad gateway</html>', { status: 502 })),
    )
    const err = (await api.get('/jobs').catch((e: unknown) => e)) as ApiError
    expect(err).toBeInstanceOf(ApiError)
    expect(err.code).toBe('error')
    expect(err.status).toBe(502)
  })
})
