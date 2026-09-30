import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Archive, Loader2 } from 'lucide-react'
import type { Application, ApplicationStatus } from '../api/types'
import { ApplicationCard } from '../components/ApplicationCard'
import { STATUS_META } from '../lib/applicationStatus'
import { useApplications } from '../hooks/useApplications'

const PIPELINE: ApplicationStatus[] = ['applied', 'phone_screen', 'interview', 'offer']
const ARCHIVE: ApplicationStatus[] = ['rejected', 'withdrawn']

export default function Tracker() {
  const { data: apps, isLoading } = useApplications()
  const [showArchive, setShowArchive] = useState(false)

  const byStatus = useMemo(() => {
    const groups = new Map<ApplicationStatus, Application[]>()
    for (const app of apps ?? []) {
      const list = groups.get(app.status) ?? []
      list.push(app)
      groups.set(app.status, list)
    }
    return groups
  }, [apps])

  const archivedCount = ARCHIVE.reduce(
    (sum, status) => sum + (byStatus.get(status)?.length ?? 0),
    0,
  )

  if (isLoading) {
    return (
      <div className="flex justify-center py-20">
        <Loader2 className="animate-spin text-ink-faint" aria-label="Loading" />
      </div>
    )
  }

  if (!apps || apps.length === 0) {
    return (
      <div className="mx-auto max-w-md py-16 text-center">
        <h1 className="headline text-3xl">
          Your applications, tracked<span className="text-go">.</span>
        </h1>
        <p className="mt-3 text-ink-soft">
          Every time you hit <strong>Apply</strong> on a job, it lands here
          automatically — move it through phone screen → interview → offer as
          things progress.
        </p>
        <Link to="/feed" className="btn-go mt-6">
          Find something to apply to
        </Link>
      </div>
    )
  }

  return (
    <div>
      <div className="mb-5 flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="headline text-3xl">
          Tracker<span className="text-go">.</span>
        </h1>
        <p className="font-mono text-xs text-ink-faint">
          {apps.length} application{apps.length === 1 ? '' : 's'} · applied →
          screen → interview → offer
        </p>
      </div>

      {/* Pipeline columns (desktop) / stacked groups (mobile) */}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {PIPELINE.map((status) => {
          const items = byStatus.get(status) ?? []
          return (
            <section key={status} aria-label={STATUS_META[status].label}>
              <header className="mb-2 flex items-center justify-between rounded-lg border border-ink bg-paper-warm px-3 py-1.5">
                <h2 className="font-mono text-xs font-bold tracking-wider uppercase">
                  {STATUS_META[status].label}
                </h2>
                <span className="font-mono text-xs text-ink-faint">{items.length}</span>
              </header>
              <div className="space-y-2">
                {items.map((app) => (
                  <ApplicationCard key={app.id} app={app} />
                ))}
                {items.length === 0 && (
                  <div className="rounded-lg border border-dashed border-rule py-6 text-center font-mono text-[10px] text-ink-faint">
                    empty
                  </div>
                )}
              </div>
            </section>
          )
        })}
      </div>

      {/* Archive: rejected / withdrawn */}
      {archivedCount > 0 && (
        <div className="mt-8">
          <button
            type="button"
            onClick={() => setShowArchive((v) => !v)}
            className="btn-quiet text-xs"
            aria-expanded={showArchive}
          >
            <Archive size={13} aria-hidden />
            Archive ({archivedCount})
          </button>
          {showArchive && (
            <div className="mt-3 grid gap-2 opacity-75 md:grid-cols-2 xl:grid-cols-3">
              {ARCHIVE.flatMap((status) => byStatus.get(status) ?? []).map((app) => (
                <ApplicationCard key={app.id} app={app} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
