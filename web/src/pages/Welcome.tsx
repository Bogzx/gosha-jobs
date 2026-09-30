import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowRight, Loader2, Sparkles } from 'lucide-react'
import { Logo } from '../components/Logo'
import { CvDropzone } from '../components/CvDropzone'
import { useToast } from '../hooks/useToast'
import { useCreateSubscription } from '../hooks/useSubscriptions'

const ROLE_PRESETS = [
  'computer science internship',
  'software engineering',
  'data science',
  'cs entry level',
  'tech internship',
]

const CITY_PRESETS = ['cluj', 'bucharest', 'timisoara', 'iasi', 'brasov', 'remote', 'romania']

export default function Welcome() {
  const navigate = useNavigate()
  const toast = useToast()
  const createSearch = useCreateSubscription()
  const [step, setStep] = useState<1 | 2>(1)
  const [roles, setRoles] = useState<string[]>(['computer science internship'])
  const [cities, setCities] = useState<string[]>([])

  const finish = () => {
    if (roles.length === 0 || cities.length === 0) {
      navigate('/feed')
      return
    }
    createSearch.mutate(
      {
        name: 'My first search',
        keywords: roles,
        locations: cities,
        experience_levels: ['intern', 'junior'],
        max_age_days: 14,
        notify_discord: true,
      },
      {
        onSuccess: () => {
          toast({ message: 'Search created — the hunt is on', tone: 'go' })
          navigate('/feed')
        },
        onError: () => navigate('/feed'),
      },
    )
  }

  const toggle = (list: string[], setList: (v: string[]) => void, value: string) => {
    setList(list.includes(value) ? list.filter((v) => v !== value) : [...list, value])
  }

  return (
    <div className="mx-auto flex min-h-dvh max-w-lg flex-col px-4 py-8">
      <Logo />

      <div className="my-auto py-10">
        <p className="mb-2 font-mono text-xs font-semibold tracking-[0.2em] text-go uppercase">
          step {step} / 2
        </p>

        {step === 1 ? (
          <div className="animate-rise">
            <h1 className="headline text-4xl">
              Welcome<span className="text-go">.</span> Let's make this
              <em className="text-moss"> yours</em>.
            </h1>
            <p className="mt-3 mb-6 text-ink-soft">
              Drop in your CV and every job gets a personal match score — the
              feed literally reads your skills. No keywords needed.
            </p>
            <CvDropzone onUploaded={() => setStep(2)} />
            <button
              type="button"
              className="mt-4 w-full text-center font-mono text-xs text-ink-faint underline-offset-2 hover:underline"
              onClick={() => setStep(2)}
            >
              skip for now — I'll add it later
            </button>
          </div>
        ) : (
          <div className="animate-rise">
            <h1 className="headline text-4xl">
              What are you hunting<span className="text-go">?</span>
            </h1>
            <p className="mt-3 mb-6 text-ink-soft">
              Pick at least one role and one place. The bot scrapes every hour
              and DMs you fresh matches on Discord.
            </p>

            <p className="mb-2 font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
              Roles
            </p>
            <div className="mb-5 flex flex-wrap gap-1.5">
              {ROLE_PRESETS.map((role) => (
                <button
                  key={role}
                  type="button"
                  aria-pressed={roles.includes(role)}
                  onClick={() => toggle(roles, setRoles, role)}
                  className={`chip cursor-pointer px-3 py-1.5 text-xs transition-colors ${
                    roles.includes(role)
                      ? 'border-moss bg-go text-white'
                      : 'hover:border-ink'
                  }`}
                >
                  {role}
                </button>
              ))}
            </div>

            <p className="mb-2 font-mono text-[11px] font-semibold tracking-wider text-ink-faint uppercase">
              Where
            </p>
            <div className="mb-8 flex flex-wrap gap-1.5">
              {CITY_PRESETS.map((city) => (
                <button
                  key={city}
                  type="button"
                  aria-pressed={cities.includes(city)}
                  onClick={() => toggle(cities, setCities, city)}
                  className={`chip cursor-pointer px-3 py-1.5 text-xs transition-colors ${
                    cities.includes(city)
                      ? 'border-moss bg-go text-white'
                      : 'hover:border-ink'
                  }`}
                >
                  {city}
                </button>
              ))}
            </div>

            <button
              type="button"
              className="btn-go w-full py-3 text-base"
              onClick={finish}
              disabled={createSearch.isPending}
            >
              {createSearch.isPending ? (
                <Loader2 size={17} className="animate-spin" aria-hidden />
              ) : (
                <Sparkles size={17} aria-hidden />
              )}
              {roles.length > 0 && cities.length > 0
                ? 'Start the hunt'
                : 'Take me to the feed'}
              <ArrowRight size={17} aria-hidden />
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
