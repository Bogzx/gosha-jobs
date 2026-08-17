import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { Job } from '../api/types'
import { JobCard } from './JobCard'

const JOB: Job = {
  id: 1,
  url: 'https://example.com/job',
  title: 'Python Developer Intern',
  company: 'CodeCorp',
  location: 'Cluj-Napoca, Romania',
  description: 'Write Python all day.',
  salary_min: 1000,
  salary_max: 1500,
  salary_currency: 'EUR',
  salary_period: 'monthly',
  salary_monthly_min_ron: 5000,
  salary_monthly_max_ron: 7500,
  source: 'indeed',
  posted_at: null,
  first_seen_at: new Date(Date.now() - 2 * 86400_000).toISOString(),
  // Raw cosine stays realistic; the badge reads the percentile.
  match_score: 0.31,
  match_percentile: 92,
  match_reasons: ['python', 'docker'],
  match_signals: [
    { kind: 'rank', text: 'Top 8% of 342 jobs ranked for you' },
    { kind: 'skill', text: 'Your CV mentions python, docker' },
    { kind: 'gap', text: 'Not in your CV: kubernetes' },
  ],
  feedback: null,
  applied: false,
}

describe('JobCard', () => {
  it('renders the key job facts', () => {
    render(<JobCard job={JOB} onSelect={() => undefined} />)
    expect(screen.getByText('Python Developer Intern')).toBeInTheDocument()
    expect(screen.getByText(/CodeCorp/)).toBeInTheDocument()
    expect(screen.getByText('92%')).toBeInTheDocument()
    expect(screen.getByText(/python, docker/)).toBeInTheDocument()
    expect(screen.getByText('Indeed')).toBeInTheDocument()
    expect(screen.getByText(/1000–1500 EUR\/mo/)).toBeInTheDocument()
    // EUR is not comparable to a Romanian monthly figure on its own.
    expect(screen.getByText(/≈5000–7500 RON\/mo/)).toBeInTheDocument()
    expect(screen.getByText('2d ago')).toBeInTheDocument()
  })

  it('shows the applied marker', () => {
    render(<JobCard job={{ ...JOB, applied: true }} onSelect={() => undefined} />)
    expect(screen.getByText('APPLIED')).toBeInTheDocument()
  })

  it('hides the match badge without a ranking', () => {
    render(
      <JobCard
        job={{ ...JOB, match_score: null, match_percentile: null }}
        onSelect={() => undefined}
      />,
    )
    expect(screen.queryByText(/%$/)).not.toBeInTheDocument()
  })

  it('shows a reachable badge tone for a strong match', () => {
    // A raw cosine of 0.31 rendered as "31%" never reached the >=75% green
    // tone; the percentile does.
    render(<JobCard job={JOB} onSelect={() => undefined} />)
    const badge = screen.getByText('92%')
    expect(badge.className).toContain('bg-go')
    expect(badge).toHaveAttribute('title', expect.stringContaining('Top'))
  })

  it('fires onSelect when clicked', async () => {
    const onSelect = vi.fn()
    render(<JobCard job={JOB} onSelect={onSelect} />)
    await userEvent.click(screen.getByRole('button'))
    expect(onSelect).toHaveBeenCalledWith(JOB)
  })
})
