import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { WhyThisMatched } from './WhyThisMatched'

describe('WhyThisMatched', () => {
  it('renders nothing when there is no explanation', () => {
    const { container } = render(<WhyThisMatched signals={null} />)
    expect(container).toBeEmptyDOMElement()

    const empty = render(<WhyThisMatched signals={[]} />)
    expect(empty.container).toBeEmptyDOMElement()
  })

  it('unpacks the badge into readable reasons', () => {
    render(
      <WhyThisMatched
        signals={[
          { kind: 'rank', text: 'Top 8% of 342 jobs ranked for you' },
          { kind: 'skill', text: 'Your CV mentions python, docker' },
          { kind: 'liked', text: 'Close to jobs you marked interested' },
        ]}
      />,
    )
    expect(screen.getByText('Why this matched')).toBeInTheDocument()
    expect(screen.getByText(/Top 8% of 342 jobs/)).toBeInTheDocument()
    expect(screen.getByText(/python, docker/)).toBeInTheDocument()
    expect(screen.getByText(/marked interested/)).toBeInTheDocument()
  })

  it('shows gaps as well as matches', () => {
    // A "why this matched" that only ever flatters the user is useless.
    render(
      <WhyThisMatched
        signals={[{ kind: 'gap', text: 'Not in your CV: kubernetes' }]}
      />,
    )
    expect(screen.getByText('Not in your CV: kubernetes')).toBeInTheDocument()
  })

  it('falls back gracefully on an unknown signal kind', () => {
    render(<WhyThisMatched signals={[{ kind: 'future', text: 'Something new' }]} />)
    expect(screen.getByText('Something new')).toBeInTheDocument()
  })
})
