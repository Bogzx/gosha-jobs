import { useEffect, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, MessageCircle } from 'lucide-react'
import { Logo } from '../components/Logo'
import { DISCORD_INVITE } from '../lib/constants'
import { prefetchLogin, signInWithDiscord } from '../lib/signin'

const GITHUB = 'https://github.com/Bogzx/gosha-jobs'

interface SamplePosting {
  title: string
  company: string
  place: string
  board: string
  match: number
  has: string[]
  gap?: string[]
  pay?: string
}

interface Sample {
  tab: string
  file: string
  language: string
  name: string
  cv: string
  postings: SamplePosting[]
}

/**
 * The hero's sample CVs (gosha/demo_personas.py, shortened) and postings
 * the demo ranked for them on 2026-10-24. Static on purpose: the landing
 * page must not wake the model on every visit; /demo is the live version.
 */
const SAMPLES: Sample[] = [
  {
    tab: 'Python backend',
    file: 'andrei-pop-cv.pdf',
    language: 'English',
    name: 'Andrei Pop',
    cv: 'Computer Science student, Technical University of Cluj-Napoca (3rd year). Backend-focused: builds APIs and data-heavy services. Skills: Python, FastAPI, Flask, Django REST Framework, SQLAlchemy, PostgreSQL, Redis, Docker, Git, Linux, pytest, REST API design, basic AWS (EC2, S3). Experience: backend intern at a small startup (summer 2025): REST endpoints in FastAPI for an invoicing app, pytest suites that took coverage from 40% to 75%. Projects: library management API (Django, PostgreSQL, Docker Compose); a compiler for a toy language.',
    postings: [
      { title: 'Working Student in Software Development', company: 'Infineon', place: 'Iași', board: 'BestJobs.ro', match: 100, has: ['python', 'backend'] },
      { title: 'Junior Full-Stack Engineer, Generative AI', company: 'Marsh', place: 'Cluj-Napoca', board: 'LinkedIn', match: 100, has: ['APIs', 'backend'] },
      { title: 'Junior C++ Embedded Systems Engineer', company: 'InfinityLabs R&D', place: 'Bucharest', board: 'LinkedIn', match: 99, has: ['APIs', 'Linux'], gap: ['embedded'] },
    ],
  },
  {
    tab: 'Frontend, CV in Romanian',
    file: 'bianca-sarbu-cv.pdf',
    language: 'Română',
    name: 'Bianca Sârbu',
    cv: 'Studentă în anul 2 la Calculatoare, Universitatea Tehnică din Cluj-Napoca. Dezvoltatoare front-end, atentă la detalii și la experiența utilizatorului. Competențe: HTML, CSS, Sass, JavaScript, TypeScript, React, Angular (de bază), Tailwind CSS, consum de API-uri REST, Git, Figma, design responsive, accesibilitate web. Experiență: am refăcut site-ul unei asociații studențești în React. Proiecte: aplicație to-do cu React și Firebase; portofoliu personal cu animații CSS.',
    postings: [
      { title: 'Web Frontend Engineer (JS, CSS, React)', company: 'Canonical', place: 'Remote', board: 'Jobicy', match: 100, has: ['web', 'design', 'React'], pay: '$48,000 to $70,000 a year' },
      { title: 'Junior Full Stack Web Developer', company: '5 Point Solutions', place: 'Iași', board: 'LinkedIn', match: 100, has: ['CSS', 'HTML', 'JavaScript'] },
      { title: 'AI Automation Developer (Junior / Mid)', company: 'Destine Holding', place: 'Suceava', board: 'eJobs.ro', match: 99, has: ['API', 'proiecte'], gap: ['automatizări'], pay: '3,500 to 10,000 RON a month' },
    ],
  },
  {
    tab: 'Data / ML',
    file: 'radu-toma-cv.pdf',
    language: 'English',
    name: 'Radu Toma',
    cv: "Master's student in Artificial Intelligence, UTCN. Interested in NLP and applied machine learning. Skills: Python, NumPy, pandas, scikit-learn, PyTorch, Hugging Face Transformers, sentence-transformers, SQL, Power BI, MLflow. Experience: research assistant in an NLP lab (2025): fine-tuned BERT models for Romanian text classification, built evaluation pipelines. Projects: semantic search over course notes with embeddings; thesis on retrieval-augmented generation.",
    postings: [
      { title: 'Machine Learning Internship 2026', company: 'Nabla', place: 'Paris', board: 'LinkedIn', match: 100, has: ['machine', 'learning'] },
      { title: 'Intern, AI Automation for Safety Engineering', company: 'NXP Semiconductors', place: 'Munich', board: 'Glassdoor', match: 100, has: ['models', 'generation'] },
      { title: 'AI/ML Engineer', company: 'Bosch Romania', place: 'Cluj-Napoca', board: 'Hipo.ro', match: 99, has: ['machine', 'learning', 'models'], gap: ['manufacturing'] },
    ],
  },
  {
    tab: 'DevOps',
    file: 'cristian-lazar-cv.pdf',
    language: 'English',
    name: 'Cristian Lazăr',
    cv: 'Computer Science student, UTCN (3rd year). Likes automating infrastructure and keeping systems running. Skills: Linux administration, Bash, Python, Docker, Kubernetes (k3s), Helm, Terraform, Ansible, GitHub Actions, AWS (EC2, S3, IAM), Nginx, Prometheus, Grafana. Experience: sysadmin intern (2024): automated user provisioning with Ansible, set up monitoring dashboards. Projects: home-lab k3s cluster with GitOps (Argo CD); AWS Certified Cloud Practitioner.',
    postings: [
      { title: 'Cloud Field Engineer', company: 'Canonical', place: 'Remote', board: 'Jobicy', match: 100, has: ['cloud', 'Linux', 'Kubernetes'] },
      { title: 'Cloud Platform Engineer', company: 'Cloud Hero', place: 'Remote', board: 'BestJobs.ro', match: 100, has: ['cloud', 'AWS', 'infrastructure'], gap: ['GCP'] },
      { title: 'DevOps Working Student, Portal Operations', company: 'IONOS', place: 'Bucharest', board: 'BestJobs.ro', match: 99, has: ['infrastructure', 'cloud', 'Linux'] },
    ],
  },
]

const BOARDS = ['Indeed', 'LinkedIn', 'Glassdoor', 'eJobs.ro', 'BestJobs.ro', 'Hipo.ro', 'RemoteOK', 'Jobicy']

const STEPS = [
  { title: 'Sign in with Discord', body: 'One click. No password and no email to confirm.' },
  { title: 'Drop in your CV', body: 'English or Romanian. The feed re-ranks every fresh posting against it straight away.' },
  { title: 'Matches come to you', body: 'New matches arrive in your Discord DMs. Apply, track and write cover letters from the same place.' },
]

const TRACKER = [
  { stage: 'Applied', cards: [['Cloud Field Engineer', 'Canonical'], ['Devops Engineer with AWS', 'Intive'], ['Cloud Platform Engineer', 'Cloud Hero']] },
  { stage: 'Screen', cards: [['DevOps Working Student', 'IONOS']] },
  { stage: 'Interview', cards: [['System Engineer (Cloud & AI)', 'Super Technologies']] },
  { stage: 'Offer', cards: [] },
]

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** The CV with every word a posting matched highlighted; `hot` are the
 * hovered posting's own. */
function MarkedCv({ text, terms, hot }: { text: string; terms: string[]; hot: string[] }) {
  const re = new RegExp(`(?<![\\p{L}])(${terms.map(escapeRe).join('|')})(?![\\p{L}])`, 'giu')
  const parts = text.split(re)
  return (
    <p className="hyphens-auto">
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <mark key={i} className={hot.includes(part.toLowerCase()) ? 'cv-mark cv-mark-hot' : 'cv-mark'}>
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </p>
  )
}

function DiscordGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden>
      <path
        fill="currentColor"
        d="M19.3 5.3A16.6 16.6 0 0 0 15.2 4l-.5 1a15.3 15.3 0 0 0-5.4 0l-.5-1a16.5 16.5 0 0 0-4.1 1.3C2.1 9.2 1.4 13 1.7 16.7a16.7 16.7 0 0 0 5 2.5l1.1-1.7a10.8 10.8 0 0 1-1.7-.8l.4-.3a11.9 11.9 0 0 0 11 0l.4.3c-.5.3-1.1.6-1.7.8l1.1 1.7a16.6 16.6 0 0 0 5-2.5c.4-4.3-.7-8-2.9-11.4ZM8.7 14.5c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2Zm6.6 0c-1 0-1.8-.9-1.8-2s.8-2 1.8-2 1.8.9 1.8 2-.8 2-1.8 2Z"
      />
    </svg>
  )
}

function Hero() {
  const [chosen, setChosen] = useState(0)
  const [hovered, setHovered] = useState<number | null>(null)
  const sample = SAMPLES[chosen]
  const terms = [...new Set(sample.postings.flatMap((p) => p.has.map((t) => t.toLowerCase())))]
  const hot = hovered == null ? [] : sample.postings[hovered].has.map((t) => t.toLowerCase())

  return (
    <section className="mx-auto max-w-6xl px-4 pt-10 md:pt-20">
      <h1 className="headline max-w-[14ch] text-[clamp(2.6rem,8.2vw,6.4rem)] leading-[0.98] text-ink">
        The job feed that <span className="hl-swipe">reads</span> your CV.
      </h1>
      <div className="mt-8 grid items-end gap-6 md:grid-cols-[minmax(0,36rem)_auto] md:gap-12">
        <p className="text-lg leading-relaxed text-ink-soft md:text-xl">
          Upload your CV once. GOSHA checks eight job boards every hour, ranks each new posting
          against your skills and your level, and sends the good ones to your Discord DMs.
        </p>
        <div className="grid justify-items-start gap-2">
          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={signInWithDiscord} className="btn-go rounded-full px-5 py-3 text-base">
              <DiscordGlyph />
              Sign in with Discord
            </button>
            <Link to="/demo" className="btn-quiet rounded-full bg-card px-5 py-3 text-base">
              See it rank a sample CV
            </Link>
          </div>
          <p className="text-xs text-ink-faint">Free. The demo needs no account and stores nothing.</p>
        </div>
      </div>

      <div className="mt-12 overflow-hidden rounded-2xl border border-rule bg-card shadow-[0_30px_60px_-40px_rgba(18,53,36,0.55)] md:mt-16">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-rule px-4 py-3">
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Sample CV">
            {SAMPLES.map((s, i) => (
              <button
                key={s.tab}
                type="button"
                aria-pressed={i === chosen}
                onClick={() => {
                  setChosen(i)
                  setHovered(null)
                }}
                className={`rounded-full border px-3.5 py-2 text-xs font-medium transition-colors ${
                  i === chosen
                    ? 'border-ink bg-ink text-paper'
                    : 'border-rule text-ink-soft hover:border-ink-faint hover:text-ink'
                }`}
              >
                {s.tab}
              </button>
            ))}
          </div>
          <span className="text-xs text-ink-faint">Sample CVs from the public demo</span>
        </div>

        <div className="grid md:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)]">
          <div className="border-b border-rule bg-paper/60 p-4 sm:p-7 md:border-r md:border-b-0">
            <article className="cv-sheet" aria-live="polite">
              <div className="mb-3 flex justify-between gap-4 border-b border-[#e3e5de] pb-2 font-sans text-[0.72rem] text-[#6c736d]">
                <span>{sample.file}</span>
                <span>{sample.language}</span>
              </div>
              <h2 className="mb-1.5 text-[1.35rem] font-semibold">{sample.name}</h2>
              <MarkedCv text={sample.cv} terms={terms} hot={hot} />
            </article>
            <p className="mt-4 text-xs text-ink-faint">
              Highlights are the CV words each posting leans on. Hover a posting to see its own.
            </p>
          </div>

          <div className="grid min-w-0 content-start gap-3 p-4 sm:p-7">
            <p className="text-xs font-semibold text-ink">Ranked for this CV</p>
            {sample.postings.map((p, i) => (
              <article
                key={p.title}
                tabIndex={0}
                onMouseEnter={() => setHovered(i)}
                onMouseLeave={() => setHovered(null)}
                onFocus={() => setHovered(i)}
                onBlur={() => setHovered(null)}
                className={`grid grid-cols-[minmax(0,1fr)_auto] gap-x-4 gap-y-1 rounded-lg border bg-card p-4 transition-colors ${
                  hovered === i ? 'border-ink-faint' : 'border-rule'
                }`}
              >
                <h3 className="font-semibold leading-snug">{p.title}</h3>
                <div className="row-span-2 text-right">
                  <span className="block text-2xl leading-none font-extrabold text-go tabular-nums [font-stretch:112%]">
                    {p.match}%
                  </span>
                  <span className="text-[0.7rem] text-ink-faint">match</span>
                </div>
                <p className="text-xs text-ink-soft">
                  {p.company}, {p.place} <span className="text-ink-faint">on {p.board}</span>
                </p>
                {p.pay && (
                  <span className="col-span-2 justify-self-start rounded-md bg-amber-soft px-2 py-0.5 text-xs font-semibold text-amber">
                    {p.pay}
                  </span>
                )}
                <p className="col-span-2 mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs">
                  <span className="font-medium text-go">Matches your {p.has.join(', ')}</span>
                  {p.gap && <span className="text-amber">Not in your CV: {p.gap.join(', ')}</span>}
                </p>
              </article>
            ))}
            <div className="flex flex-wrap items-center justify-between gap-2 pt-1 text-xs text-ink-faint">
              <span>Real postings, ranked by the demo on 24 Oct 2026.</span>
              <Link to="/demo" className="font-semibold text-go hover:underline">
                Open the live demo
              </Link>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

function DiscordDm() {
  const fields = [
    ['Level', 'Internship'],
    ['Company', 'IONOS'],
    ['Location', 'Bucharest'],
    ['Salary', 'Not listed'],
    ['Posted', '12 days ago'],
  ]
  return (
    <div
      role="img"
      aria-label="A Discord direct message from the GOSHA bot with a matched job"
      className="min-w-0 rounded-2xl bg-[#313338] p-4 text-[0.9rem] leading-snug text-[#dbdee1] shadow-[0_30px_60px_-35px_rgba(0,0,0,0.6)]"
    >
      <div className="mb-4 flex items-center gap-2 border-b border-[#3f4147] pb-3 text-[0.8rem] text-[#949ba4]">
        @ <b className="font-semibold text-[#f2f3f5]">GOSHA</b>
      </div>
      <div className="grid grid-cols-[40px_minmax(0,1fr)] gap-3">
        <img src="/gosha.jpg" alt="" className="h-10 w-10 rounded-full object-cover" />
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <b className="font-semibold text-[#f2f3f5]">GOSHA</b>
            <span className="rounded-sm bg-[#5865f2] px-1 text-[0.62rem] font-semibold text-white">APP</span>
            <span className="text-[0.72rem] text-[#949ba4]">Today at 09:14</span>
          </div>
          <div className="mt-2 max-w-md rounded border-l-4 border-[#23a55a] bg-[#2b2d31] px-3.5 py-3">
            <p className="font-semibold text-[#00a8fc]">
              DevOps Working Student for Portal Operations team (hybrid)
            </p>
            <dl className="mt-2.5 grid grid-cols-2 gap-x-4 gap-y-2.5 text-[0.82rem] sm:grid-cols-3">
              {fields.map(([name, value]) => (
                <div key={name} className="min-w-0">
                  <dt className="text-[0.78rem] font-semibold text-[#f2f3f5]">{name}</dt>
                  <dd className="break-words">{value}</dd>
                </div>
              ))}
            </dl>
            <dl className="mt-2.5 text-[0.82rem]">
              <dt className="text-[0.78rem] font-semibold text-[#f2f3f5]">Description</dt>
              <dd>
                At IONOS, the leading European provider of cloud infrastructure, cloud services and
                hosting services, you will work together with a wide range of teams…
              </dd>
            </dl>
            <p className="mt-3 text-xs text-[#949ba4]">Source: bestjobs | Strong match | Job #7787</p>
          </div>
          <div className="mt-2.5 flex flex-wrap gap-1.5 text-[0.8rem] font-medium text-white">
            <span className="rounded bg-[#23a55a] px-3 py-1.5">Apply</span>
            <span className="rounded bg-[#4e5058] px-3 py-1.5">Cover Letter</span>
            <span className="rounded bg-[#4e5058] px-3 py-1.5">Interested</span>
            <span className="rounded bg-[#da373c] px-3 py-1.5">Not Relevant</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function WhyPanel() {
  const [vote, setVote] = useState<'up' | 'down' | null>(null)
  const signals = [
    { icon: '↑', tone: 'bg-ink/10 text-ink-soft', text: 'Top 1% of 867 jobs ranked for you' },
    { icon: '✓', tone: 'bg-go-soft text-go', text: 'Your CV mentions cloud, AWS, infrastructure' },
    { icon: '!', tone: 'bg-amber-soft text-amber', text: 'Not in your CV: GCP' },
  ]
  return (
    <div className="grid gap-4 rounded-2xl border border-rule bg-card p-5 sm:p-7">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h3 className="text-lg leading-snug font-bold">Cloud Platform Engineer</h3>
          <p className="mt-0.5 text-xs text-ink-soft">Cloud Hero, Remote, BestJobs.ro, 2 days ago</p>
        </div>
        <div className="text-right">
          <span className="block text-2xl leading-none font-extrabold text-go tabular-nums [font-stretch:112%]">
            100%
          </span>
          <span className="text-[0.7rem] text-ink-faint">match</span>
        </div>
      </div>
      <ul className="grid gap-2.5 border-t border-rule pt-4">
        {signals.map((s) => (
          <li key={s.text} className="grid grid-cols-[1.6rem_1fr] items-start gap-2 text-sm">
            <span className={`grid h-5.5 w-5.5 place-items-center rounded-md text-[0.7rem] font-extrabold ${s.tone}`} aria-hidden>
              {s.icon}
            </span>
            <span>{s.text}</span>
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2" role="group" aria-label="Feedback">
        {(['up', 'down'] as const).map((v) => (
          <button
            key={v}
            type="button"
            aria-pressed={vote === v}
            onClick={() => setVote(vote === v ? null : v)}
            className={`rounded-full border px-3.5 py-2 text-xs font-medium ${
              vote === v ? 'border-go bg-go-soft text-go' : 'border-rule text-ink-soft'
            }`}
          >
            {v === 'up' ? 'More like this' : 'Not for me'}
          </button>
        ))}
      </div>
    </div>
  )
}

function Split({ title, children, art, flip = false }: {
  title: string
  children: ReactNode
  art: ReactNode
  flip?: boolean
}) {
  return (
    <section className="mx-auto max-w-6xl px-4 pt-20 md:pt-28">
      <div className="grid items-center gap-10 md:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)] md:gap-16">
        <div className={`grid max-w-xl gap-4 ${flip ? 'md:order-2' : ''}`}>
          <h2 className="headline text-[clamp(1.9rem,4vw,3rem)]">{title}</h2>
          {children}
        </div>
        {art}
      </div>
    </section>
  )
}

export default function Landing() {
  // Prefetch OAuth URLs (+ CSRF cookie) so the sign-in click can navigate
  // synchronously: mobile browsers block app deep links otherwise.
  useEffect(() => prefetchLogin(), [])

  return (
    <div className="min-h-dvh overflow-x-clip">
      <header className="sticky top-0 z-40 bg-paper/90 backdrop-blur-md">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-4">
          <Logo size="lg" />
          <nav className="flex items-center gap-6 text-sm font-medium text-ink-soft" aria-label="Main">
            <a href="#how" className="hidden hover:text-ink md:inline">How it works</a>
            <a href="#measured" className="hidden hover:text-ink md:inline">Ranking quality</a>
            <a href={GITHUB} target="_blank" rel="noopener noreferrer" className="hidden hover:text-ink md:inline">
              GitHub
            </a>
            <button type="button" onClick={signInWithDiscord} className="btn-quiet rounded-full bg-card">
              Sign in
            </button>
          </nav>
        </div>
      </header>

      <Hero />

      {/* Boards */}
      <section className="mx-auto max-w-6xl px-4 pt-16 md:pt-20">
        <div className="grid gap-6 border-t border-rule pt-8 md:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] md:gap-16">
          <div>
            <p className="font-semibold">Eight boards, one feed</p>
            <p className="mt-2 text-sm text-ink-soft">
              The international boards and the Romanian ones you would otherwise check by hand.
            </p>
          </div>
          <div>
            <p className="flex flex-wrap gap-x-5 gap-y-1 text-[clamp(1.4rem,3.2vw,2.3rem)] leading-tight font-bold tracking-[-0.015em] [font-stretch:112%]">
              {BOARDS.map((b, i) => (
                <span key={b} className={i >= 3 ? 'text-go' : undefined}>{b}</span>
              ))}
            </p>
            <p className="mt-5 max-w-2xl text-sm text-ink-soft">
              When the same ad is posted on two boards you see it once. ENGIE's Web Developer
              Junior opening in București ran on both LinkedIn and Indeed this month, and it shows
              up as a single card.
            </p>
          </div>
        </div>
      </section>

      {/* Steps: a real sequence, so numbered */}
      <section id="how" className="mx-auto max-w-6xl scroll-mt-20 px-4 pt-20 md:pt-28">
        <div className="mb-10 grid max-w-3xl gap-4">
          <h2 className="headline text-[clamp(2rem,4.6vw,3.4rem)]">Set up in under a minute.</h2>
          <p className="max-w-2xl text-lg text-ink-soft">
            No forms and no keyword lists to maintain. Your CV already says what you are looking for.
          </p>
        </div>
        <ol className="grid border-t-2 border-ink md:grid-cols-3">
          {STEPS.map((step, i) => (
            <li
              key={step.title}
              className={`grid content-start gap-2 py-5 md:pr-6 ${
                i > 0 ? 'border-t border-rule md:border-t-0 md:border-l md:pl-6' : ''
              }`}
            >
              <span className="font-extrabold text-go tabular-nums [font-stretch:125%]">{i + 1}</span>
              <h3 className="text-xl font-bold tracking-[-0.01em]">{step.title}</h3>
              <p className="max-w-[32ch] text-sm text-ink-soft">{step.body}</p>
            </li>
          ))}
        </ol>
      </section>

      <Split title="Fresh matches, straight to your DMs." art={<DiscordDm />}>
        <p className="text-ink-soft">
          The bot scrapes around the clock. A student search gets internships and junior roles; a
          posting that never says beginners can apply stays out of it.
        </p>
        <ul className="grid gap-2 text-sm">
          {[
            'Apply, Cover Letter, Interested and Not Relevant right on the message.',
            'Saved searches live on the website. Slash commands still work if you like them.',
            'Dead links are checked daily, so filled positions drop out.',
          ].map((t) => (
            <li key={t} className="grid grid-cols-[1.25rem_1fr] gap-1.5">
              <span className="mt-2 h-2 w-2 rounded-full bg-go" aria-hidden />
              {t}
            </li>
          ))}
        </ul>
      </Split>

      <Split title="Every score shows its work." art={<WhyPanel />} flip>
        <p className="text-ink-soft">
          A bare percentage is impossible to argue with. GOSHA tells you which parts of your CV a
          posting leans on, where it ranks, and what it asks for that you don't have yet.
        </p>
        <p className="text-ink-soft">
          Thumbs up and down tune your ranking: more of what you like, less of what you don't.
        </p>
      </Split>

      <Split
        title="Apply once. It's already tracked."
        art={
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4" aria-label="Application tracker example">
            {TRACKER.map((col) => (
              <div key={col.stage} className="grid min-w-0 content-start gap-2 rounded-xl bg-paper-warm p-2.5">
                <h4 className="flex justify-between text-xs font-semibold text-ink-soft">
                  {col.stage} <span className="font-normal text-ink-faint tabular-nums">{col.cards.length}</span>
                </h4>
                {col.cards.map(([title, company], i) => (
                  <div
                    key={title}
                    className={`min-w-0 rounded-lg border bg-card px-2.5 py-2 text-xs leading-snug ${
                      col.stage === 'Applied' && i === 0 ? 'border-go ring-3 ring-go-soft' : 'border-rule'
                    }`}
                  >
                    <b className="block font-semibold break-words">{title}</b>
                    <span className="text-ink-faint">{company}</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        }
      >
        <p className="text-ink-soft">
          Clicking Apply puts the job in your tracker. Move it from applied to screen, interview and
          offer, keep notes, and generate a cover letter from your CV and the posting in one click.
        </p>
      </Split>

      {/* Measured */}
      <section id="measured" className="mx-auto max-w-6xl scroll-mt-20 px-4 pt-20 md:pt-28">
        <div className="grid gap-8 rounded-3xl bg-moss p-7 text-paper sm:p-12 md:grid-cols-2 md:gap-16">
          <div className="grid content-start gap-4">
            <h2 className="headline text-[clamp(1.9rem,4vw,3rem)]">Measured, not assumed.</h2>
            <p className="text-paper/70">
              The ranking is scored on real postings labelled against sample CVs, and CI re-runs that
              evaluation on every ranking change. Results that didn't hold up are published next to
              the ones that did.
            </p>
            <a
              href={`${GITHUB}/blob/main/docs/EVAL.md`}
              target="_blank"
              rel="noopener noreferrer"
              className="font-semibold underline underline-offset-4"
            >
              Read the evaluation
            </a>
          </div>
          <div className="grid content-start gap-6">
            {[
              ['0.759', <>nDCG@10, up from <s>0.549</s> for plain embeddings</>, 'Main set: 202 postings, 14 sample CVs.'],
              ['0.604', <>nDCG@10 on a held-out set, up from <s>0.535</s></>, '261 postings, 10 CVs. The ranking was designed on the main set, so this is the number to trust, and its interval still includes zero.'],
              ['29 → 4', <>mid and senior postings reaching junior DM searches</>, 'Held-out set, after the 17 Oct 2026 rule change. The cost: fewer postings that state no level get through.'],
            ].map(([figure, label, note]) => (
              <div key={figure as string} className="grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-5 gap-y-1 border-t border-paper/20 pt-4">
                <b className="text-[clamp(2.2rem,5vw,3.2rem)] leading-none font-extrabold tracking-[-0.02em] tabular-nums [font-stretch:125%]">
                  {figure}
                </b>
                <span className="text-sm text-paper/70">{label}</span>
                <small className="col-span-2 text-xs text-paper/60">{note}</small>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Trust */}
      <section className="mx-auto max-w-6xl px-4 pt-20">
        <div className="grid gap-6 border-t border-rule pt-8 md:grid-cols-3 md:gap-8">
          {[
            ['Your CV stays yours', "CVs are encrypted at rest. Export everything you've stored or delete your account at any time from your profile."],
            ['Open source', 'The whole thing is on GitHub under AGPL-3.0. Read the ranking code, run it yourself, or send a fix.'],
            ['No tracking scripts', "Usage counts come from GOSHA's own server. No third-party analytics and no extra cookies."],
          ].map(([title, body]) => (
            <div key={title}>
              <h3 className="mb-1.5 font-bold">{title}</h3>
              <p className="text-sm text-ink-soft">{body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Closing */}
      <section className="mx-auto max-w-6xl px-4 pt-24 pb-14 md:pt-32">
        <h2 className="headline max-w-[16ch] text-[clamp(2.4rem,7vw,5.2rem)] leading-[0.98]">
          Be early to the roles worth applying for.
        </h2>
        <p className="mt-5 max-w-xl text-lg text-ink-soft">
          GOSHA checks the boards every hour, so you see a new posting the day it goes up and spend
          your time on the application instead of the search.
        </p>
        <div className="mt-8 flex flex-wrap gap-2">
          <button type="button" onClick={signInWithDiscord} className="btn-go rounded-full px-5 py-3 text-base">
            Get your feed
            <ArrowRight size={18} aria-hidden />
          </button>
          <a
            href={DISCORD_INVITE}
            target="_blank"
            rel="noopener noreferrer"
            className="btn-quiet rounded-full bg-card px-5 py-3 text-base"
          >
            <MessageCircle size={18} aria-hidden />
            Join the Discord
          </a>
        </div>
      </section>

      <footer className="border-t border-rule">
        <div className="mx-auto flex max-w-6xl flex-wrap justify-between gap-x-8 gap-y-3 px-4 pt-6 pb-10 text-xs text-ink-faint">
          <span>
            GOSHA, named after Gosha. Made by{' '}
            <a href="https://bogdantruta.com" target="_blank" rel="noopener noreferrer" className="hover:text-ink">
              Bogdan Truta
            </a>
            .
          </span>
          <nav className="flex flex-wrap gap-5" aria-label="Footer">
            <Link to="/demo" className="hover:text-ink">Demo</Link>
            <Link to="/privacy" className="hover:text-ink">Privacy</Link>
            <a href={GITHUB} target="_blank" rel="noopener noreferrer" className="hover:text-ink">GitHub</a>
            <a href={DISCORD_INVITE} target="_blank" rel="noopener noreferrer" className="hover:text-ink">Discord</a>
          </nav>
        </div>
      </footer>
    </div>
  )
}
