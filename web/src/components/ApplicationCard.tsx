import { useState } from 'react'
import { ExternalLink, NotebookPen, Trash2 } from 'lucide-react'
import type { Application, ApplicationStatus } from '../api/types'
import { useDeleteApplication, useUpdateApplication } from '../hooks/useApplications'
import { STATUS_META } from '../lib/applicationStatus'
import { sourceLabel, timeAgo } from '../lib/format'
import { useToast } from '../hooks/useToast'

const STATUS_ORDER: ApplicationStatus[] = [
  'applied', 'phone_screen', 'interview', 'offer', 'rejected', 'withdrawn',
]

export function ApplicationCard({ app }: { app: Application }) {
  const toast = useToast()
  const update = useUpdateApplication()
  const remove = useDeleteApplication()
  const [editingNotes, setEditingNotes] = useState(false)
  const [notes, setNotes] = useState(app.notes ?? '')

  const saveNotes = () => {
    setEditingNotes(false)
    if (notes !== (app.notes ?? '')) {
      update.mutate({ id: app.id, notes })
    }
  }

  return (
    <div className="card-press p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate font-semibold leading-snug">{app.job.title}</h3>
          <p className="truncate text-sm text-ink-soft">
            {app.job.company}
            <span className="text-ink-faint">
              {' '}· {sourceLabel(app.job.source)} · {timeAgo(app.applied_at)}
            </span>
          </p>
        </div>
        <a
          href={app.job.url}
          target="_blank"
          rel="noopener noreferrer"
          className="shrink-0 rounded-md border border-rule p-1.5 text-ink-soft transition-colors hover:border-ink hover:text-ink"
          aria-label="Open job posting"
        >
          <ExternalLink size={14} />
        </a>
      </div>

      <div className="mt-2.5 flex items-center gap-1.5">
        <select
          value={app.status}
          aria-label="Application status"
          onChange={(e) =>
            update.mutate({ id: app.id, status: e.target.value as ApplicationStatus })
          }
          className={`cursor-pointer rounded-md border px-2 py-1 font-mono text-xs font-bold ${STATUS_META[app.status].tone}`}
        >
          {STATUS_ORDER.map((status) => (
            <option key={status} value={status}>
              {STATUS_META[status].label}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => setEditingNotes((v) => !v)}
          className={`rounded-md border p-1.5 transition-colors ${
            app.notes || editingNotes
              ? 'border-ink bg-paper-warm text-ink'
              : 'border-rule text-ink-faint hover:border-ink hover:text-ink'
          }`}
          aria-label="Notes"
        >
          <NotebookPen size={14} />
        </button>
        <button
          type="button"
          onClick={() =>
            remove.mutate(app.id, {
              onSuccess: () => toast({ message: 'Removed from tracker' }),
            })
          }
          className="ml-auto rounded-md border border-rule p-1.5 text-ink-faint transition-colors hover:border-tomato hover:text-tomato"
          aria-label="Delete application"
        >
          <Trash2 size={14} />
        </button>
      </div>

      {(editingNotes || app.notes) && (
        <div className="mt-2">
          {editingNotes ? (
            <textarea
              autoFocus
              rows={3}
              className="input-ink text-sm"
              placeholder="Recruiter name, interview date, salary discussed…"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              onBlur={saveNotes}
            />
          ) : (
            <button
              type="button"
              onClick={() => setEditingNotes(true)}
              className="w-full rounded-md bg-paper-warm px-2.5 py-1.5 text-left text-xs leading-relaxed whitespace-pre-line text-ink-soft"
            >
              {app.notes}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
