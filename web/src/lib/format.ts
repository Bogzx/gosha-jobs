// Small formatting helpers shared across screens.

export function timeAgo(iso: string | null): string {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return ''
  const seconds = Math.max(0, (Date.now() - then) / 1000)
  if (seconds < 3600) return 'just now'
  const hours = Math.floor(seconds / 3600)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days === 1) return 'yesterday'
  if (days < 30) return `${days}d ago`
  const months = Math.floor(days / 30)
  return months === 1 ? '1mo ago' : `${months}mo ago`
}

export function formatSalary(
  min: number | null,
  max: number | null,
  currency: string | null,
): string | null {
  if (min == null && max == null) return null
  const cur = currency ?? ''
  const fmt = (n: number) =>
    n >= 10000 ? `${Math.round(n / 1000)}k` : `${Math.round(n)}`
  if (min != null && max != null && min !== max) return `${fmt(min)}–${fmt(max)} ${cur}`.trim()
  return `${fmt((min ?? max)!)} ${cur}`.trim()
}

/**
 * The badge number.
 *
 * This used to render the raw cosine similarity as a percentage. Real
 * values from a 768-dim sentence embedding land around 0.15–0.45, so the
 * badge showed "23%" for a perfectly good match and never reached the
 * ≥75% green or ≥50% amber thresholds it was styled against — nearly every
 * job in the feed rendered grey, and the one number users were meant to
 * trust said "bad match" about all of them.
 *
 * The API now returns `match_percentile`: the job's position within the
 * whole ranked candidate set (gosha/recommend.py percentile_ranks). The
 * ordering was always correct; this makes the number say the same thing
 * the ordering does.
 */
export function matchPercent(percentile: number | null): number | null {
  if (percentile == null) return null
  return Math.round(Math.min(100, Math.max(0, percentile)))
}

/** Plain-language reading of a percentile, for tooltips and detail views. */
export function matchLabel(percentile: number | null): string | null {
  if (percentile == null) return null
  if (percentile >= 75) return `Top ${Math.max(1, 100 - percentile)}% of your feed`
  if (percentile >= 50) return 'Above average for your feed'
  return `Ranks below ${100 - percentile}% of your feed`
}

export const SOURCE_LABELS: Record<string, string> = {
  indeed: 'Indeed',
  linkedin: 'LinkedIn',
  glassdoor: 'Glassdoor',
  ejobs: 'eJobs',
  bestjobs: 'BestJobs',
  hipo: 'Hipo',
  remoteok: 'RemoteOK',
}

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source.charAt(0).toUpperCase() + source.slice(1)
}
