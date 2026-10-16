import { useId, useState } from 'react'
import { X } from 'lucide-react'

interface Props {
  label: string
  values: string[]
  onChange: (values: string[]) => void
  placeholder?: string
  suggestions?: string[]
  /** A comma ends the chip. Off for places: "Cluj-Napoca, Romania" is one
   *  place, and splitting it saved the whole country as a second location. */
  commaSeparates?: boolean
}

/** Free-text chips with one-tap suggestions (smart keywords, city aliases). */
export function ChipInput({
  label, values, onChange, placeholder, suggestions = [], commaSeparates = true,
}: Props) {
  const id = useId()
  const [draft, setDraft] = useState('')

  const add = (raw: string) => {
    const value = raw.trim().toLowerCase()
    if (value && !values.includes(value)) onChange([...values, value])
    setDraft('')
  }

  const visibleSuggestions = suggestions
    .filter((s) => !values.includes(s))
    .filter((s) => !draft || s.includes(draft.toLowerCase()))
    .slice(0, 6)

  return (
    <div>
      <label htmlFor={id} className="mb-1.5 block font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
        {label}
      </label>
      <div className="input-ink flex flex-wrap items-center gap-1.5 py-1.5">
        {values.map((value) => (
          <span key={value} className="chip border-moss bg-go-soft text-moss">
            {value}
            <button
              type="button"
              aria-label={`Remove ${value}`}
              onClick={() => onChange(values.filter((v) => v !== value))}
              className="hover:text-tomato"
            >
              <X size={11} />
            </button>
          </span>
        ))}
        <input
          id={id}
          className="min-w-28 flex-1 bg-transparent text-sm outline-none placeholder:text-ink-faint"
          placeholder={values.length === 0 ? placeholder : 'add another…'}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || (commaSeparates && e.key === ',')) {
              e.preventDefault()
              add(draft)
            } else if (e.key === 'Backspace' && !draft && values.length > 0) {
              onChange(values.slice(0, -1))
            }
          }}
          onBlur={() => draft && add(draft)}
        />
      </div>
      {visibleSuggestions.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1">
          {visibleSuggestions.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              onClick={() => add(suggestion)}
              className="chip cursor-pointer transition-colors hover:border-moss hover:bg-go-soft hover:text-moss"
            >
              + {suggestion}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
