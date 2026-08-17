import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { Logo } from '../components/Logo'

/**
 * The facts come from GET /api/v1/legal/privacy rather than being written
 * into this file, so the page cannot claim something the deployment does
 * not do — most importantly *which* LLM provider a given instance ships CV
 * text to, which is an environment variable (gosha/llm.py).
 */
interface PrivacyDoc {
  controller: string
  contact: string
  data_we_hold: {
    category: string
    items: string[]
    why: string
    source: string
  }[]
  third_party_processors: {
    name: string
    operator: string
    receives: string
    url: string
    note?: string
  }[]
  llm_disclosure: string
  retention: Record<string, string>
  your_rights: Record<string, string>
  limits: Record<string, string>
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rule-dotted pt-6">
      <h2 className="headline mb-3 text-2xl">
        {title}
        <span className="text-go">.</span>
      </h2>
      {children}
    </section>
  )
}

export default function Privacy() {
  const { data, isLoading } = useQuery({
    queryKey: ['legal', 'privacy'],
    queryFn: () => api.get<PrivacyDoc>('/legal/privacy'),
    staleTime: 60 * 60_000,
  })

  return (
    <div className="mx-auto max-w-2xl px-4 py-10">
      <Link to="/" className="inline-block">
        <Logo size="lg" />
      </Link>

      <h1 className="headline mt-8 mb-2 text-4xl">
        Privacy<span className="text-go">.</span>
      </h1>
      <p className="mb-8 text-ink-soft">
        What GOSHA stores about you, who else sees it, and how to get rid of
        it. Short version: your CV is used to rank jobs, it leaves this server
        only when you personally ask for a cover letter, and you can erase
        everything with one button.
      </p>

      {isLoading || !data ? (
        <p className="font-mono text-sm text-ink-faint">loading…</p>
      ) : (
        <div className="space-y-8">
          <Section title="What we hold">
            <dl className="space-y-4">
              {data.data_we_hold.map((group) => (
                <div key={group.category}>
                  <dt className="font-semibold">{group.category}</dt>
                  <dd className="text-sm text-ink-soft">
                    <ul className="ml-4 list-disc">
                      {group.items.map((item) => (
                        <li key={item}>{item}</li>
                      ))}
                    </ul>
                    <p className="mt-1">
                      <b>Why:</b> {group.why} <b>From:</b> {group.source}
                    </p>
                  </dd>
                </div>
              ))}
            </dl>
          </Section>

          <Section title="Who else sees it">
            <div className="card-press border-amber bg-amber-soft p-4">
              <p className="text-sm leading-relaxed">{data.llm_disclosure}</p>
            </div>
            <ul className="mt-4 space-y-3">
              {data.third_party_processors.map((p) => (
                <li key={p.name} className="text-sm">
                  <b>{p.name}</b>{' '}
                  <span className="text-ink-faint">({p.operator})</span>
                  <br />
                  <span className="text-ink-soft">Receives: {p.receives}</span>
                  {p.note && (
                    <>
                      <br />
                      <span className="text-ink-faint">{p.note}</span>
                    </>
                  )}
                  {p.url && (
                    <>
                      {' '}
                      <a
                        className="text-moss underline underline-offset-2"
                        href={p.url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        their policy
                      </a>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </Section>

          <Section title="How long we keep it">
            <dl className="space-y-1.5 text-sm">
              {Object.entries(data.retention).map(([key, value]) => (
                <div key={key} className="flex flex-wrap gap-x-2">
                  <dt className="font-mono text-xs text-ink-faint">{key}</dt>
                  <dd className="text-ink-soft">{value}</dd>
                </div>
              ))}
            </dl>
          </Section>

          <Section title="Your rights">
            <p className="mb-3 text-sm text-ink-soft">
              Export and erasure both live on your{' '}
              <Link to="/profile" className="text-moss underline underline-offset-2">
                profile page
              </Link>
              .
            </p>
            <dl className="space-y-1.5 text-sm">
              {Object.entries(data.your_rights).map(([key, value]) => (
                <div key={key} className="flex flex-wrap gap-x-2">
                  <dt className="font-mono text-xs text-ink-faint">{key}</dt>
                  <dd className="text-ink-soft">{value}</dd>
                </div>
              ))}
            </dl>
          </Section>

          <Section title="Known limitations">
            <p className="mb-3 text-sm text-ink-soft">
              Stated plainly rather than omitted:
            </p>
            <ul className="ml-4 list-disc space-y-1.5 text-sm text-ink-soft">
              {Object.entries(data.limits).map(([key, value]) => (
                <li key={key}>{value}</li>
              ))}
            </ul>
          </Section>

          <p className="rule-dotted pt-6 font-mono text-xs text-ink-faint">
            Controller: {data.controller} ·{' '}
            <a
              className="underline"
              href={data.contact}
              target="_blank"
              rel="noopener noreferrer"
            >
              contact
            </a>
          </p>
        </div>
      )}
    </div>
  )
}
