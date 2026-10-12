import { beforeEach, describe, expect, it } from 'vitest'
import { forgetConsent, hasStoredConsent, storeConsent } from './cvConsent'

describe('cvConsent', () => {
  beforeEach(() => localStorage.clear())

  it("never carries one account's tick over to another", () => {
    storeConsent(1)
    expect(hasStoredConsent(1)).toBe(true)
    expect(hasStoredConsent(2)).toBe(false)
    expect(hasStoredConsent(undefined)).toBe(false)
  })

  it('ignores and clears the old browser-wide tick', () => {
    localStorage.setItem('gosha.cv-consent.v1', 'yes')
    expect(hasStoredConsent(2)).toBe(false)
    storeConsent(1)
    forgetConsent(1)
    expect(hasStoredConsent(1)).toBe(false)
    expect(localStorage.getItem('gosha.cv-consent.v1')).toBeNull()
  })
})
