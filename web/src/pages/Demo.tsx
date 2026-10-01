import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, ExternalLink, Loader2, MapPin } from 'lucide-react'
import { api } from '../api/client'
import type { Job } from '../api/types'
import { JobCard } from '../components/JobCard'
import { Logo } from '../components/Logo'
import { MatchBadge } from '../components/MatchBadge'
import { WhyThisMatched } from '../components/WhyThisMatched'
import { sourceLabel, timeAgo } from '../lib/format'
import { signInWithDiscord } from '../lib/signin'

export interface DemoPersona {
  id: string
  label: string
  summary: string
  cv: string
}

interface DemoFeed {
  persona: DemoPersona
  items: Job[]
  total: number
}

/**
 * The real feed, for a sample CV, without signing in.
 *
 * GET /api/v1/demo/feed ranks the live postings with the same code a
 * signed-in user's feed runs (gosha/recommend.py rank_candidates). The CVs
 * are synthetic and the only input is a persona id, so nothing about the
 * visitor is sent or stored.
 */
export default function Demo() {
  const personas = useQuery({
    queryKey: ['demo', 'personas'],
    queryFn: () => api.get<DemoPersona[]>('/demo/personas'),
    staleTime: Infinity,
  })
  const [chosen, setChosen] = useState<string | null>(null)
  const personaId = chosen ?? personas.data?.[0]?.id ?? null

  const feed = useQuery({
    queryKey: ['demo', 'feed', personaId],
    queryFn: () => api.get<DemoFeed>(`/demo/feed?persona=${encodeURIComponent(personaId!)}`),
    enabled: personaId != null,
    staleTime: 5 * 60_000,
  })
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const jobs = feed.data?.items ?? []
  const selected = jobs.find((job) => job.id === selectedId) ?? jobs[0] ?? null

  return (
    <div className="min-h-dvh">
      <header className="mx-auto flex max-w-6xl items-center justify-between px-4 py-5">
        <Link to="/">
          <Logo size="lg" />
        </Link>
        <button type="button" onClick={signInWithDiscord} className="btn-go text-sm">
          Get your own feed <ArrowRight size={15} aria-hidden />
        </button>
      </header>

      <main className="mx-auto max-w-6xl px-4 pb-16">
        <h1 className="headline text-4xl sm:text-5xl">
          Live demo<span className="text-go">.</span>
        </h1>
        <p className="mt-3 max-w-2xl text-ink-soft">
          Pick a sample CV and see today's postings ranked for it, by the same
          code that ranks a signed-in user's feed. The CVs are invented; nothing
          you do on this page is stored.
        </p>

        <div className="mt-6 flex flex-wrap gap-2" role="tablist" aria-label="Sample CVs">
          {personas.data?.map((p) => (
            <button
              key={p.id}
              type="button"
              role="tab"
              aria-selected={p.id === personaId}
              onClick={() => {
                setChosen(p.id)
                setSelectedId(null)
              }}
              className={`rounded-lg border px-3 py-2 text-left text-sm transition-colors ${
                p.id === personaId
                  ? 'border-ink bg-go-soft shadow-[3px_3px_0_0_var(--color-ink)]'
                  : 'border-rule bg-card hover:border-ink'
              }`}
            >
              <span className="block font-semibold">{p.label}</span>
              <span className="block text-xs text-ink-faint">{p.summary}</span>
            </button>
          ))}
        </div>

        {feed.data && (
          <details className="card-press mt-4 p-4 text-sm">
            <summary className="cursor-pointer font-semibold">
              The sample CV this feed is ranked against
            </summary>
            <p className="mt-2 leading-relaxed text-ink-soft">{feed.data.persona.cv}</p>
          </details>
        )}

        {feed.isError ? (
          <p className="card-press mt-6 p-6 text-center text-ink-soft">
            The demo is unavailable right now. Try again in a minute.
          </p>
        ) : feed.isLoading || personas.isLoading ? (
          <div className="flex justify-center py-20">
            <Loader2 className="animate-spin text-ink-faint" aria-label="Loading" />
          </div>
        ) : (
          <div className="mt-6 lg:grid lg:grid-cols-[minmax(320px,_1fr)_1.2fr] lg:items-start lg:gap-5">
            <section aria-label="Ranked jobs" className="space-y-2">
              <p className="font-mono text-xs text-ink-faint">
                top {jobs.length} of {feed.data?.total ?? 0} postings from the last 30 days
              </p>
              {jobs.map((job) => (
                <JobCard
                  key={job.id}
                  job={job}
                  selected={job.id === selected?.id}
                  onSelect={(next) => setSelectedId(next.id)}
                />
              ))}
            </section>
            {selected && <DemoDetail job={selected} />}
          </div>
        )}
      </main>
    </div>
  )
}

function DemoDetail({ job }: { job: Job }) {
  return (
    <article className="card-press sticky top-6 mt-4 p-4 lg:mt-0">
      <div className="flex items-start gap-3">
        <MatchBadge percentile={job.match_percentile} size="lg" />
        <div className="min-w-0">
          <h2 className="headline text-xl leading-tight">{job.title}</h2>
          <p className="mt-0.5 text-sm text-ink-soft">
            {job.company}
            <span className="text-ink-faint"> · </span>
            <span className="inline-flex items-center gap-0.5">
              <MapPin size={12} aria-hidden />
              {job.location || 'Unknown'}
            </span>
          </p>
        </div>
      </div>
      <WhyThisMatched signals={job.match_signals} />
      <div className="mt-3 flex flex-wrap gap-1.5">
        <span className="chip">{sourceLabel(job.source)}</span>
        <span className="chip">{timeAgo(job.posted_at ?? job.first_seen_at)}</span>
      </div>
      <a
        href={job.url}
        target="_blank"
        rel="noopener noreferrer"
        className="btn-quiet mt-4 inline-flex"
      >
        Open posting <ExternalLink size={15} aria-hidden />
      </a>
      <p className="mt-4 text-sm leading-relaxed whitespace-pre-line text-ink-soft">
        {job.description || 'No description available — open the posting for details.'}
      </p>
    </article>
  )
}
