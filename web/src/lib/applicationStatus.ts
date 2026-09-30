import type { ApplicationStatus } from '../api/types'

// Display label + colour classes per tracker status. Lives outside the
// component file so React Fast Refresh can hot-swap ApplicationCard.
export const STATUS_META: Record<
  ApplicationStatus,
  { label: string; tone: string }
> = {
  applied: { label: 'Applied', tone: 'bg-sky-soft text-sky border-sky' },
  phone_screen: { label: 'Phone screen', tone: 'bg-amber-soft text-amber border-amber' },
  interview: { label: 'Interview', tone: 'bg-amber-soft text-amber border-amber' },
  offer: { label: 'Offer 🎉', tone: 'bg-go-soft text-go border-go' },
  rejected: { label: 'Rejected', tone: 'bg-tomato-soft text-tomato border-tomato' },
  withdrawn: { label: 'Withdrawn', tone: 'bg-paper-warm text-ink-faint border-rule' },
}
