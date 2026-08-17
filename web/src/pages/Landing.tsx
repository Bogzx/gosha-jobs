import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, FileText, MessageCircle, Sparkles } from 'lucide-react'
import { Logo } from '../components/Logo'
import { DISCORD_INVITE } from '../lib/constants'
import { prefetchLogin, signInWithDiscord } from '../lib/signin'

const PHOTOS = {
  desk: 'https://images.unsplash.com/photo-1498050108023-c5249f4df085?q=80&w=1200&auto=format&fit=crop',
  team: 'https://images.unsplash.com/photo-1522202176988-66273c2fd55f?q=80&w=900&auto=format&fit=crop',
  code: 'https://images.unsplash.com/photo-1555066931-4365d14bab8c?q=80&w=900&auto=format&fit=crop',
}

const STEPS = [
  {
    icon: MessageCircle,
    title: 'Sign in with Discord',
    body: 'One click. No forms, no passwords, no email confirmations.',
  },
  {
    icon: FileText,
    title: 'Drop in your CV',
    body: 'The feed reads it and ranks every fresh posting by how well it fits you.',
  },
  {
    icon: Sparkles,
    title: 'Jobs find you',
    body: 'New matches land in your Discord DMs. Apply, track, and generate cover letters in one place.',
  },
]

export default function Landing() {
  // Prefetch OAuth URLs (+ CSRF cookie) so the sign-in click can navigate
  // synchronously: mobile browsers block app deep links otherwise.
  useEffect(() => {
    prefetchLogin()
  }, [])

  return (
    <div className="min-h-dvh overflow-x-clip">
      {/* Header */}
      <header className="mx-auto flex max-w-6xl items-center justify-between px-4 py-5">
        <Logo size="lg" />
        <button type="button" onClick={signInWithDiscord} className="btn-quiet text-sm">
          Sign in
        </button>
      </header>

      {/* Hero */}
      <section className="mx-auto grid max-w-6xl gap-10 px-4 pt-8 pb-16 md:grid-cols-[1.1fr_1fr] md:gap-6 md:pt-14">
        <div className="animate-rise">
          <p className="mb-4 font-mono text-xs font-semibold tracking-[0.2em] text-go uppercase">
            For CS students in Romania &amp; beyond
          </p>
          <h1 className="headline text-5xl text-ink sm:text-6xl lg:text-7xl">
            Stop hunting.
            <br />
            <em className="text-moss">Jobs that find you.</em>
          </h1>
          <p className="mt-6 max-w-md text-lg leading-relaxed text-ink-soft">
            Upload your CV once. GOSHA scans Indeed, LinkedIn and Romanian job
            boards around the clock, ranks every posting against your actual
            skills, and DMs you the ones worth your time.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-4">
            <button
              type="button"
              onClick={signInWithDiscord}
              className="btn-go px-6 py-3 text-base"
            >
              Sign in with Discord
              <ArrowRight size={18} aria-hidden />
            </button>
            <span className="font-mono text-xs text-ink-faint">
              free / no card / 30 seconds
            </span>
          </div>
        </div>

        {/* Pasted-photo collage */}
        <div className="relative mx-auto h-105 w-full max-w-105 animate-rise [animation-delay:0.12s]">
          <figure className="absolute top-0 left-0 w-[72%] rotate-[-2.5deg] rounded-lg border border-ink bg-card p-2 shadow-[6px_6px_0_0_var(--color-ink)]">
            <img
              src={PHOTOS.desk}
              alt="A laptop with code on the screen"
              className="aspect-[4/3] w-full rounded object-cover"
              loading="eager"
            />
            <figcaption className="px-1 pt-1.5 font-mono text-[10px] text-ink-faint">
              your next internship, found overnight
            </figcaption>
          </figure>
          <figure className="absolute right-0 bottom-14 w-[55%] rotate-[2deg] rounded-lg border border-ink bg-card p-2 shadow-[6px_6px_0_0_var(--color-ink)]">
            <img
              src={PHOTOS.team}
              alt="Students working together on laptops"
              className="aspect-[4/3] w-full rounded object-cover"
              loading="lazy"
            />
          </figure>
          {/* Floating match chip */}
          <div className="absolute bottom-2 left-6 animate-pop rounded-lg border border-moss bg-go px-4 py-2.5 font-mono text-sm font-bold text-white shadow-[4px_4px_0_0_var(--color-moss)] [animation-delay:0.5s]">
            92% match / SWE Intern / Cluj
          </div>
        </div>
      </section>

      {/* Ticker rule */}
      <div className="overflow-hidden border-y border-ink bg-moss py-2.5">
        <p className="animate-none text-center font-mono text-xs tracking-[0.25em] text-paper/90 uppercase">
          Indeed / LinkedIn / Glassdoor / eJobs / BestJobs / Hipo / Remote
          boards - one feed, zero refreshing
        </p>
      </div>

      {/* How it works */}
      <section className="mx-auto max-w-6xl px-4 py-16">
        <h2 className="headline mb-10 text-3xl sm:text-4xl">
          How it works<span className="text-go">.</span>
        </h2>
        <div className="grid gap-4 md:grid-cols-3">
          {STEPS.map(({ icon: Icon, title, body }, i) => (
            <div
              key={title}
              className="card-press card-press-hover p-6"
              style={{ animationDelay: `${i * 0.08}s` }}
            >
              <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-lg border border-ink bg-go-soft">
                <Icon size={20} className="text-moss" aria-hidden />
              </div>
              <p className="mb-1 font-mono text-xs text-ink-faint">
                0{i + 1}
              </p>
              <h3 className="headline mb-2 text-xl">{title}</h3>
              <p className="text-sm leading-relaxed text-ink-soft">{body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Closing CTA */}
      <section className="mx-auto max-w-6xl px-4 pb-20">
        <div className="relative overflow-hidden rounded-xl border border-ink bg-moss p-8 sm:p-12">
          <img
            src={PHOTOS.code}
            alt=""
            aria-hidden
            className="absolute inset-0 h-full w-full object-cover opacity-15"
            loading="lazy"
          />
          <div className="relative">
            <h2 className="headline max-w-xl text-3xl text-paper sm:text-4xl">
              The best roles get 200 applicants in the first 48 hours. Be
              early, every time.
            </h2>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={signInWithDiscord}
                className="btn-go border-paper px-6 py-3 text-base shadow-[3px_3px_0_0_var(--color-paper)]"
              >
                Get your feed
                <ArrowRight size={18} aria-hidden />
              </button>
              <a
                href={DISCORD_INVITE}
                target="_blank"
                rel="noopener noreferrer"
                className="btn-quiet border-paper/40 px-6 py-3 text-base text-paper hover:bg-paper/10"
              >
                <MessageCircle size={18} aria-hidden />
                Join the Discord
              </a>
            </div>
          </div>
        </div>
        <p className="rule-dotted mt-10 pt-6 text-center font-mono text-xs text-ink-faint">
          GOSHA.jobs - built by students, for students / gosha.bogdantruta.com
          <br />
          <a
            href={DISCORD_INVITE}
            target="_blank"
            rel="noopener noreferrer"
            className="text-moss underline underline-offset-2 hover:text-go"
          >
            join our Discord
          </a>
          {' / '}
          <Link
            to="/privacy"
            className="text-moss underline underline-offset-2 hover:text-go"
          >
            privacy
          </Link>
          {' / '}made by{' '}
          <a
            href="https://bogdantruta.com"
            target="_blank"
            rel="noopener noreferrer"
            className="text-moss underline underline-offset-2 hover:text-go"
          >
            Bogdan Truta
          </a>
        </p>
      </section>
    </div>
  )
}
