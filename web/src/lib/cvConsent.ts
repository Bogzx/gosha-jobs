// The CV consent tick, remembered per browser so it is asked once rather
// than on every re-upload. This is only a convenience: the server records
// consent itself (users.cv_consent_at) and refuses an upload without it.
// Deleting the CV withdraws consent server-side, so forget the tick too —
// otherwise the next upload would re-consent without asking.
const CONSENT_KEY = 'gosha.cv-consent.v1'

export function hasStoredConsent(): boolean {
  try {
    return window.localStorage.getItem(CONSENT_KEY) === 'yes'
  } catch {
    return false
  }
}

export function storeConsent(): void {
  try {
    window.localStorage.setItem(CONSENT_KEY, 'yes')
  } catch {
    /* private mode — the checkbox still gated this upload */
  }
}

export function forgetConsent(): void {
  try {
    window.localStorage.removeItem(CONSENT_KEY)
  } catch {
    /* nothing stored */
  }
}
