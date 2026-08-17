import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Download, LogOut, ShieldCheck, Trash2 } from 'lucide-react'
import { DiscordHelp } from '../components/DiscordHelp'
import { useToast } from '../components/Toast'
import { useLogout, useMe } from '../hooks/useMe'

const ERASE_PHRASE = 'DELETE'

export default function Profile() {
  const { me } = useMe()
  const logout = useLogout()
  const toast = useToast()
  const [erasing, setErasing] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const [busy, setBusy] = useState(false)

  if (!me) return null

  // Plain navigation rather than fetch(): the response is a file download
  // with Content-Disposition, and letting the browser handle it means no
  // blob juggling and no copy of the CV sitting in JS memory.
  const exportData = () => {
    window.location.href = '/api/v1/account/export'
  }

  const eraseAccount = async () => {
    setBusy(true)
    try {
      const resp = await fetch(`/api/v1/account?confirm=${ERASE_PHRASE}`, {
        method: 'DELETE',
        credentials: 'include',
      })
      if (!resp.ok) throw new Error('failed')
      window.location.href = '/'
    } catch {
      toast({ message: 'Could not delete the account — try again.', tone: 'tomato' })
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-md">
      <h1 className="headline mb-6 text-3xl">
        Profile<span className="text-go">.</span>
      </h1>

      <div className="card-press p-5">
        <div className="flex items-center gap-4">
          {me.avatar_url ? (
            <img
              src={me.avatar_url}
              alt=""
              className="h-16 w-16 rounded-full border-2 border-ink"
            />
          ) : (
            <span className="headline flex h-16 w-16 items-center justify-center rounded-full border-2 border-ink bg-paper-warm text-2xl">
              {(me.username ?? '?')[0]?.toUpperCase()}
            </span>
          )}
          <div>
            <p className="headline text-xl">{me.username ?? 'Anonymous'}</p>
            <p className="font-mono text-xs text-ink-faint">
              discord #{me.discord_id}
            </p>
          </div>
        </div>

        <dl className="rule-dotted mt-4 space-y-2 pt-4 text-sm">
          <div className="flex justify-between">
            <dt className="text-ink-soft">Plan</dt>
            <dd className="chip border-moss bg-go-soft font-bold text-moss uppercase">
              {me.tier}
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-ink-soft">Discord alerts</dt>
            <dd className={`font-mono text-xs font-bold ${me.in_guild ? 'text-go' : 'text-amber'}`}>
              {me.in_guild ? 'CONNECTED' : 'NOT CONNECTED'}
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-ink-soft">CV on file</dt>
            <dd className={`font-mono text-xs font-bold ${me.has_cv ? 'text-go' : 'text-amber'}`}>
              {me.has_cv ? 'YES' : 'NO'}
            </dd>
          </div>
          {me.is_admin && (
            <div className="flex justify-between">
              <dt className="text-ink-soft">Role</dt>
              <dd className="flex items-center gap-1 font-mono text-xs font-bold text-sky">
                <ShieldCheck size={13} aria-hidden /> ADMIN
              </dd>
            </div>
          )}
        </dl>
      </div>

      <div className="mt-4">
        <DiscordHelp compact={me.in_guild} />
      </div>

      <button type="button" className="btn-quiet mt-4 w-full" onClick={() => void logout()}>
        <LogOut size={15} aria-hidden />
        Sign out
      </button>

      <section className="rule-dotted mt-8 pt-6">
        <h2 className="headline mb-1 text-xl">
          Your data<span className="text-go">.</span>
        </h2>
        <p className="mb-4 text-sm text-ink-soft">
          Discord id, CV text, saved searches, delivered jobs, tracked
          applications, cover letters and the activity log. See the{' '}
          <Link to="/privacy" className="text-moss underline underline-offset-2">
            privacy notice
          </Link>{' '}
          for who else receives what.
        </p>

        <button type="button" className="btn-quiet w-full" onClick={exportData}>
          <Download size={15} aria-hidden />
          Download everything (JSON)
        </button>

        {!erasing ? (
          <button
            type="button"
            className="btn-quiet mt-2 w-full border-tomato text-tomato hover:bg-tomato-soft"
            onClick={() => setErasing(true)}
          >
            <Trash2 size={15} aria-hidden />
            Delete account
          </button>
        ) : (
          <div className="card-press mt-2 border-tomato p-4">
            <p className="mb-3 text-sm leading-relaxed">
              This erases your account, your CV, every cover letter, your saved
              searches, your tracker and your delivery history. It cannot be
              undone. Type <b className="font-mono">{ERASE_PHRASE}</b> to
              confirm.
            </p>
            <input
              className="input-ink"
              value={confirmText}
              aria-label={`Type ${ERASE_PHRASE} to confirm`}
              placeholder={ERASE_PHRASE}
              onChange={(e) => setConfirmText(e.target.value)}
            />
            <div className="mt-3 flex gap-2">
              <button
                type="button"
                className="btn-quiet flex-1"
                onClick={() => {
                  setErasing(false)
                  setConfirmText('')
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={confirmText !== ERASE_PHRASE || busy}
                className="btn-quiet flex-1 border-tomato text-tomato hover:bg-tomato-soft disabled:cursor-not-allowed disabled:opacity-40"
                onClick={() => void eraseAccount()}
              >
                {busy ? 'Deleting…' : 'Delete forever'}
              </button>
            </div>
          </div>
        )}
      </section>

      <p className="rule-dotted mt-8 pt-4 text-center font-mono text-[11px] leading-relaxed text-ink-faint">
        Made by{' '}
        <a
          className="underline"
          href="https://bogdantruta.com"
          target="_blank"
          rel="noopener noreferrer"
        >
          Bogdan Truta
        </a>
      </p>
    </div>
  )
}
