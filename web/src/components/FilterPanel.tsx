import { X } from 'lucide-react'
import type { JobFilters } from '../api/types'

const LOCATION_PRESETS = [
  'Cluj', 'Bucharest', 'Timisoara', 'Iasi', 'Brasov', 'Romania', 'Europe',
]
const EXPERIENCE_LEVELS = ['intern', 'junior', 'mid', 'senior']
const SOURCES = ['indeed', 'linkedin', 'glassdoor', 'ejobs', 'bestjobs', 'hipo', 'remoteok']
const POSTED_OPTIONS = [
  { label: 'Any time', value: 0 },
  { label: 'Last 24h', value: 1 },
  { label: 'Last 3 days', value: 3 },
  { label: 'Last week', value: 7 },
  { label: 'Last 2 weeks', value: 14 },
]

interface Props {
  filters: JobFilters
  onChange: (filters: JobFilters) => void
}

function toggleCsv(csv: string | undefined, value: string): string {
  const items = (csv ?? '').split(',').filter(Boolean)
  const next = items.includes(value)
    ? items.filter((item) => item !== value)
    : [...items, value]
  return next.join(',')
}

function csvHas(csv: string | undefined, value: string): boolean {
  return (csv ?? '').split(',').includes(value)
}

export function FilterPanel({ filters, onChange }: Props) {
  const set = (patch: Partial<JobFilters>) => onChange({ ...filters, ...patch })

  const hasAny =
    !!filters.q ||
    !!filters.locations ||
    !!filters.experience ||
    !!filters.sources ||
    !!filters.salary_min ||
    !!filters.posted_within_days ||
    !!filters.remote

  return (
    <div className="space-y-5">
      <div>
        <label htmlFor="filter-q" className="mb-1.5 block font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
          Search
        </label>
        <input
          id="filter-q"
          className="input-ink"
          placeholder="title or company…"
          value={filters.q ?? ''}
          onChange={(e) => set({ q: e.target.value || undefined })}
        />
      </div>

      <div>
        <p className="mb-1.5 font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
          Location
        </p>
        <div className="flex flex-wrap gap-1.5">
          {LOCATION_PRESETS.map((loc) => {
            const active = csvHas(filters.locations, loc.toLowerCase())
            return (
              <button
                key={loc}
                type="button"
                aria-pressed={active}
                onClick={() =>
                  set({ locations: toggleCsv(filters.locations, loc.toLowerCase()) || undefined })
                }
                className={`chip cursor-pointer transition-colors ${
                  active ? 'border-moss bg-go text-white' : 'hover:border-ink'
                }`}
              >
                {loc}
              </button>
            )
          })}
          <button
            type="button"
            aria-pressed={!!filters.remote}
            onClick={() => set({ remote: !filters.remote || undefined })}
            className={`chip cursor-pointer transition-colors ${
              filters.remote ? 'border-moss bg-go text-white' : 'hover:border-ink'
            }`}
          >
            Remote OK
          </button>
        </div>
      </div>

      <div>
        <p className="mb-1.5 font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
          Level
        </p>
        <div className="flex flex-wrap gap-1.5">
          {EXPERIENCE_LEVELS.map((level) => {
            const active = csvHas(filters.experience, level)
            return (
              <button
                key={level}
                type="button"
                aria-pressed={active}
                onClick={() =>
                  set({ experience: toggleCsv(filters.experience, level) || undefined })
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

      <div>
        <p className="mb-1.5 font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
          Source
        </p>
        <div className="flex flex-wrap gap-1.5">
          {SOURCES.map((source) => {
            const active = csvHas(filters.sources, source)
            return (
              <button
                key={source}
                type="button"
                aria-pressed={active}
                onClick={() =>
                  set({ sources: toggleCsv(filters.sources, source) || undefined })
                }
                className={`chip cursor-pointer transition-colors ${
                  active ? 'border-moss bg-go text-white' : 'hover:border-ink'
                }`}
              >
                {source}
              </button>
            )
          })}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <label htmlFor="filter-posted" className="mb-1.5 block font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
            Posted
          </label>
          <select
            id="filter-posted"
            className="input-ink"
            value={filters.posted_within_days ?? 0}
            onChange={(e) =>
              set({ posted_within_days: Number(e.target.value) || undefined })
            }
          >
            {POSTED_OPTIONS.map(({ label, value }) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="filter-salary" className="mb-1.5 block font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
            Min RON / month
          </label>
          <input
            id="filter-salary"
            type="number"
            min={0}
            step={500}
            className="input-ink"
            placeholder="any"
            // Everything is compared as gross RON per month, converted at
            // ingest, so a monthly-EUR and an annual-USD posting are judged
            // on the same scale (gosha/salary.py).
            title="Gross RON per month. Listings in EUR or USD, and annual figures, are converted before filtering. Jobs that don't state a salary are always included."
            value={filters.salary_min ?? ''}
            onChange={(e) =>
              set({ salary_min: Number(e.target.value) || undefined })
            }
          />
          <p className="mt-1 font-mono text-[10px] leading-tight text-ink-faint">
            other currencies converted
          </p>
        </div>
      </div>

      {hasAny && (
        <button
          type="button"
          onClick={() => onChange({})}
          className="btn-quiet w-full text-xs"
        >
          <X size={13} aria-hidden /> Clear all filters
        </button>
      )}
    </div>
  )
}
