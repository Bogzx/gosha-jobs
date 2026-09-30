import { useState } from 'react'
import {
  BellOff,
  BellRing,
  Loader2,
  Pause,
  Pencil,
  Play,
  Plus,
  Trash2,
} from 'lucide-react'
import type { Subscription } from '../api/types'
import { DiscordHelp } from '../components/DiscordHelp'
import { SearchForm } from '../components/SearchForm'
import { useToast } from '../hooks/useToast'
import { useMe } from '../hooks/useMe'
import {
  useDeleteSubscription,
  useSubscriptions,
  useToggleSubscription,
} from '../hooks/useSubscriptions'

function SearchCard({ sub, onEdit }: { sub: Subscription; onEdit: () => void }) {
  const toast = useToast()
  const toggle = useToggleSubscription()
  const remove = useDeleteSubscription()

  return (
    <div className={`card-press p-4 ${sub.is_active ? '' : 'opacity-60'}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="font-semibold">
            {sub.name || sub.keywords.slice(0, 2).join(', ')}
            {!sub.is_active && (
              <span className="ml-2 font-mono text-[10px] text-amber uppercase">paused</span>
            )}
          </h3>
          <p className="mt-0.5 text-sm text-ink-soft">
            {sub.keywords.join(', ')}
          </p>
        </div>
        <span
          className="shrink-0"
          title={sub.notify_discord ? 'Discord alerts on' : 'Discord alerts off'}
        >
          {sub.notify_discord ? (
            <BellRing size={16} className="text-go" aria-label="Alerts on" />
          ) : (
            <BellOff size={16} className="text-ink-faint" aria-label="Alerts off" />
          )}
        </span>
      </div>

      <div className="mt-2.5 flex flex-wrap gap-1.5">
        {sub.locations.map((loc) => (
          <span key={loc} className="chip">{loc}</span>
        ))}
        {sub.remote_ok && <span className="chip">remote ok</span>}
        {sub.experience_levels.filter((l) => l !== 'any').map((level) => (
          <span key={level} className="chip border-amber bg-amber-soft text-amber">
            {level}
          </span>
        ))}
        {sub.salary_min != null && (
          <span className="chip">≥ {sub.salary_min}</span>
        )}
        <span className="chip border-transparent bg-transparent">
          last {sub.max_age_days}d
        </span>
      </div>

      <div className="rule-dotted mt-3 flex items-center gap-1.5 pt-3">
        <button type="button" className="btn-quiet px-2.5 py-1 text-xs" onClick={onEdit}>
          <Pencil size={13} aria-hidden /> Edit
        </button>
        <button
          type="button"
          className="btn-quiet px-2.5 py-1 text-xs"
          onClick={() => toggle.mutate({ id: sub.id, active: !sub.is_active })}
        >
          {sub.is_active ? (
            <><Pause size={13} aria-hidden /> Pause</>
          ) : (
            <><Play size={13} aria-hidden /> Resume</>
          )}
        </button>
        <button
          type="button"
          aria-label="Delete search"
          className="ml-auto rounded-md border border-rule p-1.5 text-ink-faint transition-colors hover:border-tomato hover:text-tomato"
          onClick={() => {
            if (window.confirm('Delete this search? Alerts for it will stop.')) {
              remove.mutate(sub.id, {
                onSuccess: () => toast({ message: 'Search deleted' }),
              })
            }
          }}
        >
          <Trash2 size={14} />
        </button>
      </div>
    </div>
  )
}

export default function Searches() {
  const { me } = useMe()
  const { data: subs, isLoading } = useSubscriptions()
  const [editing, setEditing] = useState<Subscription | 'new' | null>(null)

  if (isLoading) {
    return (
      <div className="flex justify-center py-20">
        <Loader2 className="animate-spin text-ink-faint" aria-label="Loading" />
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-2xl">
      <div className="mb-5 flex items-center justify-between">
        <h1 className="headline text-3xl">
          Searches<span className="text-go">.</span>
        </h1>
        {editing === null && (
          <button type="button" className="btn-go" onClick={() => setEditing('new')}>
            <Plus size={15} aria-hidden /> New search
          </button>
        )}
      </div>

      {me && !me.in_guild && (
        <div className="mb-4">
          <DiscordHelp />
        </div>
      )}

      {editing !== null ? (
        <div className="card-press p-5">
          <h2 className="headline mb-4 text-xl">
            {editing === 'new' ? 'New search' : 'Edit search'}
          </h2>
          <SearchForm
            existing={editing === 'new' ? undefined : editing}
            onDone={() => setEditing(null)}
          />
        </div>
      ) : subs && subs.length > 0 ? (
        <div className="space-y-3">
          {subs.map((sub) => (
            <SearchCard key={sub.id} sub={sub} onEdit={() => setEditing(sub)} />
          ))}
        </div>
      ) : (
        <div className="card-press p-8 text-center">
          <h2 className="headline text-xl">No saved searches yet</h2>
          <p className="mx-auto mt-2 max-w-sm text-sm text-ink-soft">
            A search tells GOSHA what to hunt for — keywords plus cities. The
            bot scrapes every hour and DMs you fresh matches.
          </p>
          <button type="button" className="btn-go mt-5" onClick={() => setEditing('new')}>
            <Plus size={15} aria-hidden /> Create your first search
          </button>
        </div>
      )}
    </div>
  )
}
