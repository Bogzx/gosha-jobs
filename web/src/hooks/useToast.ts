import { createContext, useContext } from 'react'

// The toast context and its hook, kept apart from the ToastProvider
// component so React Fast Refresh can hot-swap Toast.tsx.
export interface ToastInput {
  message: string
  tone?: 'ink' | 'go' | 'tomato'
  action?: { label: string; onClick: () => void }
  durationMs?: number
}

export const ToastContext = createContext<(toast: ToastInput) => void>(() => undefined)

export function useToast() {
  return useContext(ToastContext)
}
