import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Loader2, SlidersHorizontal, Sparkles } from 'lucide-react'
import type { Job, JobFilters } from '../api/types'
import { FilterPanel } from '../components/FilterPanel'
import { JobCard } from '../components/JobCard'
import { JobDetail } from '../components/JobDetail'
import { useMe } from '../hooks/useMe'
import { useJobList } from '../hooks/useJobs'

type Mode = 'feed' | 'browse'

export default function Feed() {
  const { me } = useMe()
  const [mode, setMode] = useState<Mode>('feed')
  const [filters, setFilters] = useState<JobFilters>({})
  const [selected, setSelected] = useState<Job | null>(null)
  const [sheetOpen, setSheetOpen] = useState(false)

  const query = useJobList(mode, filters)
  const jobs = useMemo(
    () => query.data?.pages.flatMap((page) => page.items) ?? [],
    [query.data],
  )
  const total = query.data?.pages[0]?.total

  // Desktop shows the first job until the user picks one, so the detail
  // pane is never empty. Derived during render: setting state from an
  // effect cost an extra render pass on every list change.
  const [isDesktop] = useState(() => window.matchMedia('(min-width: 1024px)').matches)

  // Keep the detail pane in sync with fresh data (feedback/applied flags)
  const selectedFresh = useMemo(() => {
    const current = selected ?? (isDesktop ? (jobs[0] ?? null) : null)
    return jobs.find((job) => job.id === current?.id) ?? current
  }, [jobs, selected, isDesktop])

  return (
    <div className="lg:grid lg:grid-cols-[230px_minmax(340px,_1fr)_1.2fr] lg:items-start lg:gap-5">
      {/* ── Filters: desktop sidebar ── */}
      <aside className="sticky top-20 hidden lg:block">
        <h2 className="headline mb-4 text-lg">Filters</h2>
        <FilterPanel
          filters={filters}
          onChange={(next) => {
            setFilters(next)
            setMode('browse')
          }}
        />
      </aside>

      {/* ── Job list ── */}
      <section aria-label="Job list">
        <div className="mb-3 flex items-center justify-between gap-2">
          <div className="flex rounded-lg border border-ink p-0.5">
            {(
              [
                ['feed', 'For you'],
                ['browse', 'All jobs'],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => {
                  setMode(value)
                  setSelected(null)
                }}
                aria-pressed={mode === value}
                className={`rounded-md px-3 py-1 text-sm font-semibold transition-colors ${
                  mode === value ? 'bg-moss text-paper' : 'text-ink-soft hover:text-ink'
                }`}
              >
                {value === 'feed' && <Sparkles size={13} className="mr-1 inline" aria-hidden />}
                {label}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            {total != null && (
              <span className="font-mono text-xs text-ink-faint">{total} jobs</span>
            )}
            <button
              type="button"
              className="btn-quiet px-2.5 py-1.5 lg:hidden"
              onClick={() => setSheetOpen(true)}
              aria-label="Open filters"
            >
              <SlidersHorizontal size={15} aria-hidden /> Filters
            </button>
          </div>
        </div>

        {mode === 'feed' && me && !me.has_cv && (
          <Link
            to="/cv"
            className="card-press card-press-hover mb-3 block border-dashed p-4"
          >
            <p className="font-semibold">
              <Sparkles size={15} className="mr-1 inline text-go" aria-hidden />
              Upload your CV to unlock personal match scores
            </p>
            <p className="mt-0.5 text-sm text-ink-soft">
              Right now you're seeing the newest jobs. With a CV, every posting
              gets ranked against your actual skills. Takes 30 seconds →
            </p>
          </Link>
        )}

        {query.isLoading ? (
          <div className="flex justify-center py-20">
            <Loader2 className="animate-spin text-ink-faint" aria-label="Loading" />
          </div>
        ) : jobs.length === 0 ? (
          <div className="card-press p-8 text-center">
            <p className="headline text-xl">Nothing here yet</p>
            <p className="mt-1 text-sm text-ink-soft">
              {mode === 'browse'
                ? 'Try loosening the filters.'
                : 'The scraper runs hourly — check back soon, or create a search so we know what to hunt for.'}
            </p>
          </div>
        ) : (
          <ul className="space-y-2">
            {jobs.map((job) => (
              <li key={job.id}>
                <JobCard
                  job={job}
                  selected={selectedFresh?.id === job.id}
                  onSelect={setSelected}
                />
              </li>
            ))}
          </ul>
        )}

        {query.hasNextPage && (
          <button
            type="button"
            onClick={() => void query.fetchNextPage()}
            disabled={query.isFetchingNextPage}
            className="btn-quiet mt-4 w-full"
          >
            {query.isFetchingNextPage ? (
              <Loader2 size={15} className="animate-spin" aria-hidden />
            ) : (
              'Load more'
            )}
          </button>
        )}
      </section>

      {/* ── Detail: desktop pane ── */}
      <aside className="sticky top-20 hidden h-[calc(100dvh-6.5rem)] lg:block">
        {selectedFresh ? (
          <div className="card-press h-full overflow-hidden">
            <JobDetail job={selectedFresh} />
          </div>
        ) : (
          <div className="flex h-full items-center justify-center rounded-lg border border-dashed border-rule">
            <p className="font-mono text-xs text-ink-faint">select a job</p>
          </div>
        )}
      </aside>

      {/* ── Detail: mobile slide-over ── */}
      {selectedFresh && (
        <div className="fixed inset-0 z-50 lg:hidden" role="dialog" aria-modal>
          <button
            type="button"
            aria-label="Close details"
            className="absolute inset-0 bg-ink/40"
            onClick={() => setSelected(null)}
          />
          <div className="absolute inset-x-0 top-12 bottom-0 animate-slide-up overflow-hidden rounded-t-xl border border-ink bg-card">
            <JobDetail job={selectedFresh} onClose={() => setSelected(null)} />
          </div>
        </div>
      )}

      {/* ── Filters: mobile bottom sheet ── */}
      {sheetOpen && (
        <div className="fixed inset-0 z-50 lg:hidden" role="dialog" aria-modal>
          <button
            type="button"
            aria-label="Close filters"
            className="absolute inset-0 bg-ink/40"
            onClick={() => setSheetOpen(false)}
          />
          <div className="absolute inset-x-0 bottom-0 max-h-[80dvh] animate-slide-up overflow-y-auto rounded-t-xl border border-ink bg-card p-5 pb-10">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="headline text-lg">Filters</h2>
              <button
                type="button"
                className="btn-ink px-3 py-1 text-xs"
                onClick={() => setSheetOpen(false)}
              >
                Done
              </button>
            </div>
            <FilterPanel
              filters={filters}
              onChange={(next) => {
                setFilters(next)
                setMode('browse')
              }}
            />
          </div>
        </div>
      )}
    </div>
  )
}
