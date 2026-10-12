// The CV consent tick, remembered per browser *and per account* so it is
// asked once rather than on every re-upload. This is only a convenience:
// the server records consent itself (users.cv_consent_at) and refuses an
// upload without it. Deleting the CV withdraws consent server-side, so
// forget the tick too — otherwise the next upload would re-consent without
// asking. Keyed by user id and dropped on sign-out, so on a shared computer
// one person's tick never uploads for the next.
const LEGACY_KEY = 'gosha.cv-consent.v1'
const keyFor = (userId: number) => `gosha.cv-consent.v2.${userId}`

export function hasStoredConsent(userId: number | undefined): boolean {
  if (userId == null) return false
  try {
    return window.localStorage.getItem(keyFor(userId)) === 'yes'
  } catch {
    return false
  }
}

export function storeConsent(userId: number | undefined): void {
  if (userId == null) return
  try {
    window.localStorage.setItem(keyFor(userId), 'yes')
  } catch {
    /* private mode — the checkbox still gated this upload */
  }
}

export function forgetConsent(userId: number | undefined): void {
  try {
    // The v1 tick belonged to whoever used this browser first.
    window.localStorage.removeItem(LEGACY_KEY)
    if (userId != null) window.localStorage.removeItem(keyFor(userId))
  } catch {
    /* nothing stored */
  }
}
