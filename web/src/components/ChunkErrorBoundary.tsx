import { Component, type ReactNode } from 'react'
import { isChunkLoadError } from '../lib/chunkError'

// sessionStorage key holding the time of the last automatic reload.
const RELOADED_AT = 'gosha:chunk-reload-at'
// A failure this soon after our own reload means reloading does not help
// (the network, not a stale tab): stop and show the fallback.
const RELOAD_COOLDOWN_MS = 60_000

function recentlyReloaded(): boolean {
  try {
    const at = Number(sessionStorage.getItem(RELOADED_AT))
    return Number.isFinite(at) && Date.now() - at < RELOAD_COOLDOWN_MS
  } catch {
    return false
  }
}

function markReloaded(): void {
  try {
    sessionStorage.setItem(RELOADED_AT, String(Date.now()))
  } catch {
    // Storage blocked: still reload; the cooldown just cannot be enforced.
  }
}

interface Props {
  children: ReactNode
  /** Injected in tests; the page reload otherwise. */
  reload?: () => void
}

/**
 * Pages are lazy-loaded with content-hashed file names, so a deploy removes
 * the files an already-open tab will ask for next: navigating then threw
 * inside Suspense and left a blank page. A failed chunk import now reloads
 * the page once (the new index.html names the new files); anything else,
 * or a second failure right after that reload, shows a way out instead of
 * nothing.
 */
export class ChunkErrorBoundary extends Component<Props, { error: unknown }> {
  state: { error: unknown } = { error: null }

  static getDerivedStateFromError(error: unknown) {
    return { error }
  }

  componentDidCatch(error: unknown) {
    if (isChunkLoadError(error) && !recentlyReloaded()) {
      markReloaded()
      ;(this.props.reload ?? (() => window.location.reload()))()
    }
  }

  render() {
    if (this.state.error == null) return this.props.children
    return (
      <div className="mx-auto flex min-h-dvh max-w-xl flex-col items-start justify-center gap-4 px-4">
        <h1 className="headline text-3xl">
          Something went wrong<span className="text-go">.</span>
        </h1>
        <p className="text-ink-soft">
          {isChunkLoadError(this.state.error)
            ? 'GOSHA was updated while this tab was open, and part of the new version could not be loaded.'
            : 'This page hit an unexpected error.'}
        </p>
        <button type="button" className="btn-go" onClick={() => window.location.reload()}>
          Reload
        </button>
      </div>
    )
  }
}
