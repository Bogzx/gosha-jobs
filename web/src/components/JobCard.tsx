import { CheckCircle2, MapPin } from 'lucide-react'
import type { Job } from '../api/types'
import { formatSalary, sourceLabel, timeAgo } from '../lib/format'
import { MatchBadge } from './MatchBadge'

interface Props {
  job: Job
  selected?: boolean
  onSelect: (job: Job) => void
}

export function JobCard({ job, selected = false, onSelect }: Props) {
  const salary = formatSalary(job.salary_min, job.salary_max, job.salary_currency)

  return (
    <button
      type="button"
      onClick={() => onSelect(job)}
      aria-pressed={selected}
      className={`group w-full rounded-lg border p-3 text-left transition-all ${
        selected
          ? 'border-ink bg-go-soft shadow-[3px_3px_0_0_var(--color-ink)]'
          : 'border-rule bg-card hover:-translate-y-0.5 hover:border-ink hover:shadow-[3px_3px_0_0_var(--color-ink)]'
      } ${job.feedback === 'not_relevant' ? 'opacity-45' : ''}`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate font-semibold leading-snug">{job.title}</h3>
          <p className="truncate text-sm text-ink-soft">
            {job.company}
            <span className="text-ink-faint"> · </span>
            <span className="inline-flex items-center gap-0.5 text-ink-faint">
              <MapPin size={11} className="inline shrink-0" aria-hidden />
              <span className="truncate">{job.location || 'Unknown'}</span>
            </span>
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <MatchBadge percentile={job.match_percentile} />
          {job.applied && (
            <span
              className="inline-flex items-center gap-1 font-mono text-[10px] font-bold text-go"
              title="In your tracker"
            >
              <CheckCircle2 size={12} aria-hidden /> APPLIED
            </span>
          )}
        </div>
      </div>

      {job.match_reasons && job.match_reasons.length > 0 && (
        <p className="mt-1.5 truncate text-xs text-go">
          ✦ matches your {job.match_reasons.join(', ')}
        </p>
      )}

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <span className="chip">{sourceLabel(job.source)}</span>
        {salary && <span className="chip border-amber bg-amber-soft text-amber">{salary}</span>}
        <span className="chip border-transparent bg-transparent">
          {timeAgo(job.posted_at ?? job.first_seen_at)}
        </span>
      </div>
    </button>
  )
}
