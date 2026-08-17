import { matchLabel, matchPercent } from '../lib/format'

/**
 * The hero metric: where this job sits among everything ranked for you.
 *
 * The value is a percentile, not a raw similarity — see matchPercent in
 * lib/format.ts for why. Because percentiles are uniform by construction,
 * the ≥75 / ≥50 thresholds below now mean what they look like they mean:
 * top quarter green, next quarter amber, bottom half quiet.
 */
export function MatchBadge({
  percentile,
  size = 'sm',
}: {
  percentile: number | null
  size?: 'sm' | 'lg'
}) {
  const percent = matchPercent(percentile)
  if (percent == null) return null

  const tone =
    percent >= 75
      ? 'bg-go text-white border-moss'
      : percent >= 50
        ? 'bg-amber-soft text-amber border-amber'
        : 'bg-paper-warm text-ink-faint border-rule'

  return (
    <span
      className={`inline-flex items-center rounded-md border font-mono font-bold ${tone} ${
        size === 'lg' ? 'px-2.5 py-1 text-sm' : 'px-1.5 py-0.5 text-xs'
      }`}
      title={matchLabel(percentile) ?? undefined}
    >
      {percent}%
    </span>
  )
}
