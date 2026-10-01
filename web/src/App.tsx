import { lazy, Suspense, useEffect, type ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { trackPageview } from './api/client'
import { Layout } from './components/Layout'
import { useMe } from './hooks/useMe'
import Landing from './pages/Landing'

// Landing is the only page every visitor needs, so it is the only one in
// the entry bundle. The rest load on navigation — notably Admin, which
// alone pulls in Recharts (~half the old single 713 KB bundle).
const Feed = lazy(() => import('./pages/Feed'))
const Tracker = lazy(() => import('./pages/Tracker'))
const Searches = lazy(() => import('./pages/Searches'))
const CvPage = lazy(() => import('./pages/CvPage'))
const Profile = lazy(() => import('./pages/Profile'))
const Admin = lazy(() => import('./pages/Admin'))
const Welcome = lazy(() => import('./pages/Welcome'))
const SignedIn = lazy(() => import('./pages/SignedIn'))
const Privacy = lazy(() => import('./pages/Privacy'))
const Demo = lazy(() => import('./pages/Demo'))
const NotFound = lazy(() => import('./pages/NotFound'))

function Loading() {
  return (
    <div className="flex min-h-dvh items-center justify-center">
      <span className="headline animate-pulse text-2xl text-ink-faint">…</span>
    </div>
  )
}

function usePageviews() {
  const location = useLocation()
  useEffect(() => {
    trackPageview(location.pathname)
  }, [location.pathname])
}

function Protected({ children }: { children: ReactNode }) {
  const { me, isLoading } = useMe()
  if (isLoading) return <Loading />
  if (!me) return <Navigate to="/" replace />
  return <Layout>{children}</Layout>
}

export default function App() {
  usePageviews()
  const { me, isLoading } = useMe()

  return (
    <Suspense fallback={<Loading />}>
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
      <Route path="/demo" element={<Demo />} />
      <Route path="/feed" element={<Protected><Feed /></Protected>} />
      <Route path="/tracker" element={<Protected><Tracker /></Protected>} />
      <Route path="/searches" element={<Protected><Searches /></Protected>} />
      <Route path="/cv" element={<Protected><CvPage /></Protected>} />
      <Route path="/profile" element={<Protected><Profile /></Protected>} />
      <Route path="/admin" element={<Protected><Admin /></Protected>} />
      <Route path="*" element={<NotFound />} />
    </Routes>
    </Suspense>
  )
}
