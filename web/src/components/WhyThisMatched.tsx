import { AlertCircle, Sparkles, ThumbsUp, TrendingUp } from 'lucide-react'
import type { MatchSignal } from '../api/types'

const ICONS = {
  rank: TrendingUp,
  skill: Sparkles,
  liked: ThumbsUp,
  gap: AlertCircle,
} as const

const TONES = {
  rank: 'text-ink-soft',
  skill: 'text-go',
  liked: 'text-sky',
  gap: 'text-amber',
} as const

/**
 * Unpacks the match badge.
 *
 * A single opaque percentage is not something anyone can act on or argue
 * with. The ranking already computes which CV terms the posting leans on,
 * whether the feedback nudge is what lifted it, and where it sits in the
 * candidate set — all of which used to be discarded before it reached the
 * screen. The `gap` signal is deliberately included: a feature that only
 * ever tells you what you already have is flattering and useless.
 */
export function WhyThisMatched({ signals }: { signals: MatchSignal[] | null }) {
  if (!signals || signals.length === 0) return null

  return (
    <section className="mt-3 rounded-lg border border-rule bg-paper-warm p-3">
      <h3 className="mb-2 font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
        Why this matched
      </h3>
      <ul className="space-y-1.5">
        {signals.map((signal) => {
          const Icon = ICONS[signal.kind as keyof typeof ICONS] ?? Sparkles
          const tone = TONES[signal.kind as keyof typeof TONES] ?? 'text-ink-soft'
          return (
            <li
              key={`${signal.kind}:${signal.text}`}
              className="flex items-start gap-2 text-sm leading-snug"
            >
              <Icon size={14} className={`mt-0.5 shrink-0 ${tone}`} aria-hidden />
              <span className={signal.kind === 'gap' ? 'text-ink-soft' : ''}>
                {signal.text}
              </span>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
