import { useEffect, type ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { trackPageview } from './api/client'
import { Layout } from './components/Layout'
import { useMe } from './hooks/useMe'
import Landing from './pages/Landing'
import Feed from './pages/Feed'
import Tracker from './pages/Tracker'
import Searches from './pages/Searches'
import CvPage from './pages/CvPage'
import Profile from './pages/Profile'
import Admin from './pages/Admin'
import Welcome from './pages/Welcome'
import SignedIn from './pages/SignedIn'
import Privacy from './pages/Privacy'

function usePageviews() {
  const location = useLocation()
  useEffect(() => {
    trackPageview(location.pathname)
  }, [location.pathname])
}

function Protected({ children }: { children: ReactNode }) {
  const { me, isLoading } = useMe()
  if (isLoading) {
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <span className="headline animate-pulse text-2xl text-ink-faint">…</span>
      </div>
    )
  }
  if (!me) return <Navigate to="/" replace />
  return <Layout>{children}</Layout>
}

export default function App() {
  usePageviews()
  const { me, isLoading } = useMe()

  return (
    <Routes>
      <Route
        path="/"
        element={
          !isLoading && me ? <Navigate to="/feed" replace /> : <Landing />
        }
      />
      <Route path="/welcome" element={<Welcome />} />
      <Route path="/signed-in" element={<SignedIn />} />
      <Route path="/privacy" element={<Privacy />} />
      <Route path="/feed" element={<Protected><Feed /></Protected>} />
      <Route path="/tracker" element={<Protected><Tracker /></Protected>} />
      <Route path="/searches" element={<Protected><Searches /></Protected>} />
      <Route path="/cv" element={<Protected><CvPage /></Protected>} />
      <Route path="/profile" element={<Protected><Profile /></Protected>} />
      <Route path="/admin" element={<Protected><Admin /></Protected>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
