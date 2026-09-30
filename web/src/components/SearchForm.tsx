import { useState } from 'react'
import { Loader2 } from 'lucide-react'
import type { Subscription, SubscriptionInput } from '../api/types'
import { ApiError } from '../api/client'
import {
  useCreateSubscription,
  useMeta,
  useUpdateSubscription,
} from '../hooks/useSubscriptions'
import { ChipInput } from './ChipInput'
import { useToast } from '../hooks/useToast'

const EXPERIENCE_OPTIONS = ['intern', 'junior', 'mid', 'senior'] as const

interface Props {
  existing?: Subscription
  onDone: () => void
}

export function SearchForm({ existing, onDone }: Props) {
  const toast = useToast()
  const { data: meta } = useMeta()
  const create = useCreateSubscription()
  const update = useUpdateSubscription()

  const [name, setName] = useState(existing?.name ?? '')
  const [keywords, setKeywords] = useState<string[]>(existing?.keywords ?? [])
  const [locations, setLocations] = useState<string[]>(existing?.locations ?? [])
  const [excluded, setExcluded] = useState<string[]>(existing?.excluded_keywords ?? [])
  const [blacklist, setBlacklist] = useState<string[]>(existing?.company_blacklist ?? [])
  const [experience, setExperience] = useState<string[]>(
    existing?.experience_levels.filter((l) => l !== 'any') ?? ['intern', 'junior'],
  )
  const [remoteOk, setRemoteOk] = useState(existing?.remote_ok ?? false)
  const [salaryMin, setSalaryMin] = useState<string>(
    existing?.salary_min != null ? String(existing.salary_min) : '',
  )
  const [maxAge, setMaxAge] = useState(existing?.max_age_days ?? 14)
  const [notify, setNotify] = useState(existing?.notify_discord ?? true)

  const pending = create.isPending || update.isPending

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (keywords.length === 0 || locations.length === 0) {
      toast({ message: 'Add at least one keyword and one location.', tone: 'tomato' })
      return
    }
    const input: SubscriptionInput = {
      name: name.trim() || null,
      keywords,
      locations,
      excluded_keywords: excluded,
      company_blacklist: blacklist,
      experience_levels: experience.length > 0 ? experience : ['any'],
      remote_ok: remoteOk,
      salary_min: salaryMin ? Number(salaryMin) : null,
      max_age_days: maxAge,
      notify_discord: notify,
    }
    const onError = (err: unknown) => {
      toast({
        message: err instanceof ApiError ? err.message : 'Something went wrong.',
        tone: 'tomato',
      })
    }
    if (existing) {
      update.mutate({ id: existing.id, ...input }, {
        onSuccess: () => {
          toast({ message: 'Search updated', tone: 'go' })
          onDone()
        },
        onError,
      })
    } else {
      create.mutate(input, {
        onSuccess: () => {
          toast({ message: 'Search created — alerts are live', tone: 'go' })
          onDone()
        },
        onError,
      })
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4">
      <div>
        <label htmlFor="search-name" className="mb-1.5 block font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
          Name <span className="normal-case">(optional)</span>
        </label>
        <input
          id="search-name"
          className="input-ink"
          placeholder="e.g. Cluj internships"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>

      <ChipInput
        label="Keywords"
        values={keywords}
        onChange={setKeywords}
        placeholder="software engineer intern, qa…"
        suggestions={meta?.smart_keywords}
      />

      <ChipInput
        label="Locations"
        values={locations}
        onChange={setLocations}
        placeholder="cluj, bucharest, remote…"
        suggestions={meta?.locations}
      />

      <div>
        <p className="mb-1.5 font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
          Experience level
        </p>
        <div className="flex flex-wrap gap-1.5">
          {EXPERIENCE_OPTIONS.map((level) => {
            const active = experience.includes(level)
            return (
              <button
                key={level}
                type="button"
                aria-pressed={active}
                onClick={() =>
                  setExperience((prev) =>
                    active ? prev.filter((l) => l !== level) : [...prev, level],
                  )
                }
                className={`chip cursor-pointer transition-colors ${
                  active ? 'border-moss bg-go text-white' : 'hover:border-ink'
                }`}
              >
                {level}
              </button>
            )
          })}
        </div>
      </div>

      <details className="group">
        <summary className="cursor-pointer font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase select-none">
          More options ▾
        </summary>
        <div className="mt-3 space-y-4 border-l-2 border-rule pl-4">
          <ChipInput
            label="Exclude words"
            values={excluded}
            onChange={setExcluded}
            placeholder="unpaid, sales, php…"
          />
          <ChipInput
            label="Blocked companies"
            values={blacklist}
            onChange={setBlacklist}
            placeholder="companies you never want to see"
          />
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label htmlFor="search-salary" className="mb-1.5 block font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
                Min salary
              </label>
              <input
                id="search-salary"
                type="number"
                min={0}
                className="input-ink"
                placeholder="any"
                value={salaryMin}
                onChange={(e) => setSalaryMin(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="search-age" className="mb-1.5 block font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
                Max job age
              </label>
              <select
                id="search-age"
                className="input-ink"
                value={maxAge}
                onChange={(e) => setMaxAge(Number(e.target.value))}
              >
                <option value={3}>3 days</option>
                <option value={7}>1 week</option>
                <option value={14}>2 weeks</option>
                <option value={30}>1 month</option>
              </select>
            </div>
          </div>
          <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
            <input
              type="checkbox"
              checked={remoteOk}
              onChange={(e) => setRemoteOk(e.target.checked)}
              className="h-4 w-4 accent-(--color-go)"
            />
            Also include remote jobs
          </label>
        </div>
      </details>

      <label className="flex cursor-pointer items-center gap-2 rounded-lg border border-rule bg-paper-warm px-3 py-2.5 text-sm font-medium">
        <input
          type="checkbox"
          checked={notify}
          onChange={(e) => setNotify(e.target.checked)}
          className="h-4 w-4 accent-(--color-go)"
        />
        DM me on Discord when new matches appear
      </label>

      <div className="flex gap-2">
        <button type="submit" className="btn-go flex-1" disabled={pending}>
          {pending && <Loader2 size={15} className="animate-spin" aria-hidden />}
          {existing ? 'Save changes' : 'Create search'}
        </button>
        <button type="button" className="btn-quiet" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  )
}
