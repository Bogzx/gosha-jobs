// Thin fetch wrapper for /api/v1: JSON, cookies, error envelope -> ApiError.

export class ApiError extends Error {
  code: string
  status: number

  constructor(status: number, code: string, message: string) {
    super(message)
    this.code = code
    this.status = status
  }
}

const BASE = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    ...init,
  })

  let body: unknown = null
  const text = await resp.text()
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = null
    }
  }

  if (!resp.ok) {
    const err = (body as { error?: { code?: string; message?: string } })?.error
    throw new ApiError(
      resp.status,
      err?.code ?? 'error',
      err?.message ?? `Request failed (${resp.status})`,
    )
  }
  return body as T
}

export const api = {
  get<T>(path: string): Promise<T> {
    return request<T>(path)
  },

  post<T>(path: string, data?: unknown): Promise<T> {
    return request<T>(path, {
      method: 'POST',
      headers: data === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: data === undefined ? undefined : JSON.stringify(data),
    })
  },

  patch<T>(path: string, data: unknown): Promise<T> {
    return request<T>(path, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    })
  },

  delete<T>(path: string): Promise<T> {
    return request<T>(path, { method: 'DELETE' })
  },

  putFile<T>(path: string, file: File, fields: Record<string, string> = {}): Promise<T> {
    const form = new FormData()
    form.append('file', file)
    for (const [name, value] of Object.entries(fields)) form.append(name, value)
    return request<T>(path, { method: 'PUT', body: form })
  },
}

/** Fire-and-forget pageview ping; analytics must never break the app. */
export function trackPageview(path: string): void {
  void api.post('/events/pageview', { path }).catch(() => undefined)
}
