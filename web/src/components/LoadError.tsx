import { Loader2, RotateCw } from 'lucide-react'

interface Props {
  /** What failed to load, e.g. "your applications". */
  what: string
  onRetry: () => void
  retrying?: boolean
}

/**
 * A failed fetch, said plainly. Without it a 5xx (say, mid-deploy) renders
 * like an empty account: "no applications yet", an upload box as if there
 * were no CV.
 */
export function LoadError({ what, onRetry, retrying = false }: Props) {
  return (
    <div role="alert" className="card-press p-8 text-center">
      <p className="headline text-xl">Couldn't load {what}</p>
      <p className="mt-1 text-sm text-ink-soft">
        The server may be restarting — give it a moment and try again.
      </p>
      <button
        type="button"
        className="btn-quiet mt-4"
        onClick={onRetry}
        disabled={retrying}
      >
        {retrying ? (
          <Loader2 size={15} className="animate-spin" aria-hidden />
        ) : (
          <RotateCw size={15} aria-hidden />
        )}
        Try again
      </button>
    </div>
  )
}
