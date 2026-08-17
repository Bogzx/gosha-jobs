// Discord sign-in that opens the app where the platform allows it.
//
// Reality check (per Discord's API team, discord-api-docs#1296/#7259):
// for OAuth flows with identity scopes, Discord INTENTIONALLY does not
// deep link into the mobile app — the browser authorize page is the
// supported path on Android. Their assetlinks.json does declare
// get_login_creds, so Chrome can autofill Discord credentials from
// Google Password Manager, and after the first login the discord.com
// session persists, making future sign-ins one tap.
//
// Per platform:
//  - Android: straight https navigation to the authorize page.
//  - iOS: plain https navigation; Discord declares /oauth2/authorize as a
//    Universal Link, so iOS may hand it to the app when installed.
//  - Desktop: discord:// attempt (the desktop app accepts it) + timed
//    fallback to the web flow, canceled when the app steals focus.
//
// The custom-scheme attempt must run SYNCHRONOUSLY inside the click
// gesture, hence the prefetched URLs (+ CSRF state cookie) on page load.

const LOGIN_PATH = '/api/v1/auth/discord/login'
const HANDOFF_PATH = '/api/v1/auth/discord/handoff'
// State cookie lives 10 min; refresh the prefetched URLs well within that.
const REFRESH_MS = 8 * 60 * 1000
// Cross-browser handoff: the callback may land in the system default
// browser, which gets no session (that would be the login-CSRF hole).
// This tab holds the state cookie, so it collects the sign-in itself.
const HANDOFF_POLL_MS = 2000
const HANDOFF_WINDOW_MS = 5 * 60 * 1000

interface LoginUrls {
  web_url: string
  app_url: string
}

let cached: LoginUrls | null = null
let refreshTimer: number | undefined

async function fetchUrls(): Promise<LoginUrls | null> {
  try {
    const resp = await fetch(`${LOGIN_PATH}?format=json`, { credentials: 'include' })
    if (!resp.ok) return null
    return (await resp.json()) as LoginUrls
  } catch {
    return null
  }
}

/** Call on page load so the click handler can navigate synchronously. */
export function prefetchLogin(): void {
  void fetchUrls().then((urls) => {
    cached = urls
  })
  if (refreshTimer === undefined) {
    refreshTimer = window.setInterval(() => {
      void fetchUrls().then((urls) => {
        if (urls) cached = urls
      })
    }, REFRESH_MS)
  }
}

let handoffTimer: number | undefined

/** Poll for a sign-in that completed in another browser, then land the user. */
export function startHandoffPolling(): () => void {
  if (handoffTimer !== undefined) return () => undefined
  const deadline = Date.now() + HANDOFF_WINDOW_MS

  const stop = () => {
    if (handoffTimer !== undefined) {
      window.clearInterval(handoffTimer)
      handoffTimer = undefined
    }
  }

  handoffTimer = window.setInterval(() => {
    if (Date.now() > deadline) {
      stop()
      return
    }
    void fetch(HANDOFF_PATH, { credentials: 'include' })
      .then((r) => (r.ok ? r.json() : null))
      .then((body: { signed_in?: boolean; new_user?: boolean } | null) => {
        if (!body?.signed_in) return
        stop()
        window.location.href = body.new_user ? '/welcome' : '/feed'
      })
      .catch(() => undefined)
  }, HANDOFF_POLL_MS)

  return stop
}

/** Synchronous within the click gesture — required for app deep links. */
export function signInWithDiscord(): void {
  const urls = cached
  if (!urls) {
    // Prefetch hasn't landed (or failed) — plain browser flow always works.
    window.location.href = LOGIN_PATH
    return
  }

  const ua = navigator.userAgent
  const isMobile =
    /android|iphone|ipad|ipod/i.test(ua) ||
    // iPadOS reports as Mac but has touch
    (/macintosh/i.test(ua) && navigator.maxTouchPoints > 1)
  if (isMobile) {
    // Browser authorize page (iOS may hand it to the app via Universal Link)
    window.location.href = urls.web_url
    return
  }

  // Desktop: try the app scheme, fall back to the web flow unless the
  // app visibly took over. The app hands the callback to the system
  // default browser, so this tab watches for the handoff.
  startHandoffPolling()
  const fallback = window.setTimeout(() => {
    window.location.href = urls.web_url
  }, 1600)
  const cancel = () => window.clearTimeout(fallback)
  document.addEventListener(
    'visibilitychange',
    () => {
      if (document.hidden) cancel()
    },
    { once: true },
  )
  window.addEventListener('pagehide', cancel, { once: true })
  window.addEventListener('blur', cancel, { once: true })

  window.location.href = urls.app_url
}
