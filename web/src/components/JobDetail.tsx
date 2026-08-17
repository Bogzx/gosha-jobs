import { useState } from 'react'
import {
  Copy,
  ExternalLink,
  Loader2,
  MapPin,
  PenLine,
  ThumbsDown,
  ThumbsUp,
  X,
} from 'lucide-react'
import type { Job } from '../api/types'
import { ApiError } from '../api/client'
import {
  useApplyClick,
  useCoverLetter,
  useFeedback,
  useJobDetail,
  useUndoApply,
} from '../hooks/useJobs'
import {
  formatMonthlyRon,
  formatSalary,
  sourceLabel,
  timeAgo,
} from '../lib/format'
import { MatchBadge } from './MatchBadge'
import { WhyThisMatched } from './WhyThisMatched'
import { useToast } from './Toast'

interface Props {
  job: Job
  onClose?: () => void
}

export function JobDetail({ job, onClose }: Props) {
  const toast = useToast()
  const feedback = useFeedback()
  const applyClick = useApplyClick()
  const undoApply = useUndoApply()
  const coverLetter = useCoverLetter()
  const [letter, setLetter] = useState<string | null>(null)

  // List payloads truncate descriptions for bandwidth; fetch the full
  // record so the pane never shows a "…" cut-off.
  const detail = useJobDetail(job.id)
  const description = detail.data?.description ?? job.description

  const salary = formatSalary(
    job.salary_min, job.salary_max, job.salary_currency, job.salary_period,
  )
  const comparable =
    job.salary_currency && job.salary_currency.toUpperCase() !== 'RON'
      ? formatMonthlyRon(job.salary_monthly_min_ron, job.salary_monthly_max_ron)
      : null

  const handleApply = () => {
    window.open(job.url, '_blank', 'noopener')
    if (job.applied) return
    applyClick.mutate(job.id, {
      onSuccess: (app) => {
        toast({
          message: 'Added to your tracker',
          tone: 'go',
          action: {
            label: 'Undo',
            onClick: () =>
              undoApply.mutate({ applicationId: app.id, jobId: job.id }),
          },
        })
      },
    })
  }

  const handleLetter = () => {
    setLetter(null)
    coverLetter.mutate(job.id, {
      onSuccess: ({ content }) => setLetter(content),
      onError: (err) => {
        const message =
          err instanceof ApiError
            ? err.code === 'no_cv'
              ? 'Upload your CV first — the letter needs it.'
              : err.message
            : 'Generation failed — try again.'
        toast({ message, tone: 'tomato' })
      },
    })
  }

  const react = (value: 'interested' | 'not_relevant') => {
    feedback.mutate({
      jobId: job.id,
      feedback: value,
    })
    if (value === 'not_relevant') {
      toast({ message: 'Got it — fewer jobs like this.' })
    } else {
      toast({ message: 'Nice — more jobs like this.', tone: 'go' })
    }
  }

  return (
    <article className="flex h-full flex-col">
      <header className="border-b border-rule p-4">
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="float-right rounded-md border border-rule p-1.5 hover:border-ink lg:hidden"
          >
            <X size={16} />
          </button>
        )}
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
          {salary && (
            <span className="chip border-amber bg-amber-soft text-amber">
              {salary}
              {comparable && (
                <span className="ml-1 font-normal opacity-70">{comparable}</span>
              )}
            </span>
          )}
          <span className="chip">{timeAgo(job.posted_at ?? job.first_seen_at)}</span>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <button type="button" onClick={handleApply} className="btn-go">
            {job.applied ? 'Open posting' : 'Apply'}
            <ExternalLink size={15} aria-hidden />
          </button>
          <button
            type="button"
            onClick={handleLetter}
            className="btn-quiet"
            disabled={coverLetter.isPending}
          >
            {coverLetter.isPending ? (
              <Loader2 size={15} className="animate-spin" aria-hidden />
            ) : (
              <PenLine size={15} aria-hidden />
            )}
            Cover letter
          </button>
          <div className="ml-auto flex gap-1">
            <button
              type="button"
              aria-label="More like this"
              onClick={() => react('interested')}
              className={`rounded-md border p-2 transition-colors ${
                job.feedback === 'interested'
                  ? 'border-moss bg-go text-white'
                  : 'border-rule hover:border-ink'
              }`}
            >
              <ThumbsUp size={15} />
            </button>
            <button
              type="button"
              aria-label="Not relevant"
              onClick={() => react('not_relevant')}
              className={`rounded-md border p-2 transition-colors ${
                job.feedback === 'not_relevant'
                  ? 'border-ink bg-tomato text-white'
                  : 'border-rule hover:border-ink'
              }`}
            >
              <ThumbsDown size={15} />
            </button>
          </div>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {letter && (
          <section className="card-press mb-4 p-4">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="headline text-base">Your cover letter</h3>
              <button
                type="button"
                className="btn-quiet px-2 py-1 text-xs"
                onClick={() => {
                  void navigator.clipboard.writeText(letter)
                  toast({ message: 'Copied to clipboard', tone: 'go' })
                }}
              >
                <Copy size={13} aria-hidden /> Copy
              </button>
            </div>
            <p className="text-sm leading-relaxed whitespace-pre-line text-ink-soft">
              {letter}
            </p>
          </section>
        )}

        <p className="text-[15px] leading-relaxed whitespace-pre-line">
          {description || 'No description available — open the posting for details.'}
        </p>
      </div>
    </article>
  )
}
