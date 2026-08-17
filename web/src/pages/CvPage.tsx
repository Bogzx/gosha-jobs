import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Copy, FileCheck2, Loader2, Trash2 } from 'lucide-react'
import { CvDropzone } from '../components/CvDropzone'
import { useToast } from '../components/Toast'
import { useCoverLetters, useCv, useDeleteCv } from '../hooks/useCv'
import { timeAgo } from '../lib/format'

export default function CvPage() {
  const toast = useToast()
  const { data: cv, isLoading } = useCv()
  const { data: letters } = useCoverLetters()
  const deleteCv = useDeleteCv()
  const [openLetter, setOpenLetter] = useState<number | null>(null)

  if (isLoading) {
    return (
      <div className="flex justify-center py-20">
        <Loader2 className="animate-spin text-ink-faint" aria-label="Loading" />
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-2xl space-y-8">
      <section>
        <h1 className="headline mb-1 text-3xl">
          Your CV<span className="text-go">.</span>
        </h1>
        <p className="mb-5 text-sm text-ink-soft">
          One upload powers everything: match scores in your feed and AI cover
          letters per job. Stored as plain text on our server (not encrypted
          at rest). Deleting it here removes the file, the match vector and
          every cover letter generated from it — your saved searches,
          applications and delivery history stay. To remove those too, use{' '}
          <Link to="/profile" className="text-moss underline underline-offset-2">
            delete account
          </Link>
          . Full detail in the{' '}
          <Link to="/privacy" className="text-moss underline underline-offset-2">
            privacy notice
          </Link>
          .
        </p>

        {cv?.has_cv ? (
          <div className="card-press overflow-hidden">
            <div className="flex items-center justify-between border-b border-rule bg-go-soft px-4 py-3">
              <p className="flex items-center gap-2 font-semibold">
                <FileCheck2 size={18} className="text-go" aria-hidden />
                CV on file
                <span className="font-mono text-xs font-normal text-ink-faint">
                  {cv.uploaded_at ? `updated ${timeAgo(cv.uploaded_at)}` : ''}
                </span>
              </p>
              <button
                type="button"
                className="rounded-md border border-rule p-1.5 text-ink-faint transition-colors hover:border-tomato hover:text-tomato"
                aria-label="Delete CV"
                onClick={() => {
                  if (window.confirm('Delete your CV? This also deletes every cover letter generated from it, and match scores stop working. This cannot be undone.')) {
                    deleteCv.mutate(undefined, {
                      onSuccess: () => toast({ message: 'CV deleted' }),
                    })
                  }
                }}
              >
                <Trash2 size={15} />
              </button>
            </div>
            <pre className="max-h-72 overflow-y-auto p-4 font-sans text-sm leading-relaxed whitespace-pre-wrap text-ink-soft">
              {cv.text}
            </pre>
            <div className="border-t border-rule p-3">
              <CvDropzone />
            </div>
          </div>
        ) : (
          <CvDropzone />
        )}
      </section>

      <section>
        <h2 className="headline mb-1 text-2xl">
          Cover letters<span className="text-go">.</span>
        </h2>
        <p className="mb-4 text-sm text-ink-soft">
          Generated from any job's detail view. Cached for 30 days.
        </p>
        {letters && letters.length > 0 ? (
          <ul className="space-y-2">
            {letters.map((letter) => (
              <li key={letter.id} className="card-press overflow-hidden">
                <button
                  type="button"
                  className="flex w-full items-baseline justify-between gap-2 p-3 text-left"
                  aria-expanded={openLetter === letter.id}
                  onClick={() =>
                    setOpenLetter(openLetter === letter.id ? null : letter.id)
                  }
                >
                  <span className="min-w-0">
                    <span className="block truncate font-semibold">
                      {letter.job_title}
                    </span>
                    <span className="text-sm text-ink-soft">{letter.company}</span>
                  </span>
                  <span className="shrink-0 font-mono text-xs text-ink-faint">
                    {timeAgo(letter.created_at)}
                  </span>
                </button>
                {openLetter === letter.id && (
                  <div className="border-t border-rule p-3">
                    <button
                      type="button"
                      className="btn-quiet mb-2 px-2 py-1 text-xs"
                      onClick={() => {
                        void navigator.clipboard.writeText(letter.content)
                        toast({ message: 'Copied to clipboard', tone: 'go' })
                      }}
                    >
                      <Copy size={13} aria-hidden /> Copy
                    </button>
                    <p className="text-sm leading-relaxed whitespace-pre-line text-ink-soft">
                      {letter.content}
                    </p>
                  </div>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p className="rounded-lg border border-dashed border-rule p-6 text-center font-mono text-xs text-ink-faint">
            none yet — open a job and hit “Cover letter”
          </p>
        )}
      </section>
    </div>
  )
}
