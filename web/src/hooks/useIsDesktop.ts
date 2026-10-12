import { useSyncExternalStore } from 'react'

// Tailwind's `lg` breakpoint: the width at which pages switch from stacked
// sheets to side-by-side panes.
const DESKTOP_QUERY = '(min-width: 1024px)'

function subscribe(onChange: () => void) {
  const mql = window.matchMedia(DESKTOP_QUERY)
  mql.addEventListener('change', onChange)
  return () => mql.removeEventListener('change', onChange)
}

/** True at `lg` and up; follows the window as it is resized. */
export function useIsDesktop(): boolean {
  return useSyncExternalStore(subscribe, () => window.matchMedia(DESKTOP_QUERY).matches)
}
