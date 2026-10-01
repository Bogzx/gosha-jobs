import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { isChunkLoadError } from '../lib/chunkError'
import { ChunkErrorBoundary } from './ChunkErrorBoundary'

function Throws({ error }: { error: Error }): never {
  throw error
}

const STALE_CHUNK = new TypeError(
  'Failed to fetch dynamically imported module: https://gosha.test/assets/Feed-abc123.js',
)

describe('ChunkErrorBoundary', () => {
  beforeEach(() => {
    sessionStorage.clear()
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => vi.restoreAllMocks())

  it('recognises failed chunk imports across browsers', () => {
    expect(isChunkLoadError(STALE_CHUNK)).toBe(true)
    expect(isChunkLoadError(new TypeError('Importing a module script failed.'))).toBe(true)
    expect(isChunkLoadError(new Error('error loading dynamically imported module'))).toBe(true)
    expect(isChunkLoadError(new Error('Cannot read properties of undefined'))).toBe(false)
  })

  it('reloads once on a stale chunk, then shows a way out instead of looping', () => {
    const reload = vi.fn()
    const { unmount } = render(
      <ChunkErrorBoundary reload={reload}>
        <Throws error={STALE_CHUNK} />
      </ChunkErrorBoundary>,
    )
    expect(reload).toHaveBeenCalledTimes(1)
    unmount()

    // Same failure right after that reload: no second reload.
    render(
      <ChunkErrorBoundary reload={reload}>
        <Throws error={STALE_CHUNK} />
      </ChunkErrorBoundary>,
    )
    expect(reload).toHaveBeenCalledTimes(1)
    expect(screen.getByText(/updated while this tab was open/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument()
  })

  it('does not reload for ordinary errors', () => {
    const reload = vi.fn()
    render(
      <ChunkErrorBoundary reload={reload}>
        <Throws error={new Error('boom')} />
      </ChunkErrorBoundary>,
    )
    expect(reload).not.toHaveBeenCalled()
    expect(screen.getByText(/unexpected error/)).toBeInTheDocument()
  })
})
