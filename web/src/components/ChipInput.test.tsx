import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ChipInput } from './ChipInput'

describe('ChipInput', () => {
  it('ends a chip at a comma by default (keywords)', async () => {
    const onChange = vi.fn()
    render(<ChipInput label="Keywords" values={[]} onChange={onChange} />)
    await userEvent.type(screen.getByLabelText('Keywords'), 'java,')
    expect(onChange).toHaveBeenLastCalledWith(['java'])
  })

  it('keeps "City, Country" as one place when commas do not separate', async () => {
    const onChange = vi.fn()
    render(<ChipInput label="Locations" values={[]} onChange={onChange} commaSeparates={false} />)
    await userEvent.type(screen.getByLabelText('Locations'), 'Cluj-Napoca, Romania{Enter}')
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenLastCalledWith(['cluj-napoca, romania'])
  })
})
