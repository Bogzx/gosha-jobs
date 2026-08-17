import { useEffect } from 'react'
import { CheckCircle2 } from 'lucide-react'
import { Logo } from '../components/Logo'
import { startHandoffPolling } from '../lib/signin'

/**
 * Landing spot for an OAuth callback that arrived in a different browser
 * than the one that started the sign-in (the desktop Discord app opens
 * links in the system default browser).
 *
 * This browser deliberately gets NO session: handing one out to whoever
 * opens a callback URL is the login-CSRF hole (gosha/api/auth.py). The tab
 * that started the flow holds the state cookie and collects the sign-in
 * from /auth/discord/handoff. If that tab happens to be this one, the poll
 * below picks it up immediately and moves on.
 */
export default function SignedIn() {
  useEffect(() => startHandoffPolling(), [])

  return (
    <div className="mx-auto flex min-h-dvh max-w-lg flex-col items-center justify-center px-4 text-center">
      <Logo size="lg" />
      <CheckCircle2 size={40} className="mt-8 text-go" aria-hidden />
      <h1 className="headline mt-4 text-3xl">
        Discord approved<span className="text-go">.</span>
      </h1>
      <p className="mt-3 text-ink-soft">
        Head back to the window where you clicked <b>Sign in</b> — it is
        finishing up and will drop you straight into your feed.
      </p>
      <p className="mt-6 max-w-sm font-mono text-xs leading-relaxed text-ink-faint">
        We only ever sign in the browser that started the request. It means one
        extra hop when Discord opens a different browser, and it means nobody
        can hand you a link that logs you into their account.
      </p>
      <a href="/" className="btn-quiet mt-8 text-sm">
        Back to gosha
      </a>
    </div>
  )
}
