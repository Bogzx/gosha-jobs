import { Link } from 'react-router-dom'
import { Logo } from '../components/Logo'

export default function NotFound() {
  return (
    <div className="mx-auto flex min-h-dvh max-w-xl flex-col items-start justify-center gap-4 px-4">
      <Link to="/">
        <Logo size="lg" />
      </Link>
      <h1 className="headline text-4xl">
        Page not found<span className="text-go">.</span>
      </h1>
      <p className="text-ink-soft">
        There is nothing at this address. Try the{' '}
        <Link to="/" className="text-moss underline underline-offset-2">
          home page
        </Link>{' '}
        or the{' '}
        <Link to="/demo" className="text-moss underline underline-offset-2">
          live demo
        </Link>
        .
      </p>
    </div>
  )
}
