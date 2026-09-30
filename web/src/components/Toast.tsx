import { useCallback, useRef, useState, type ReactNode } from 'react'
import { ToastContext, type ToastInput } from '../hooks/useToast'

interface ToastItem extends ToastInput {
  id: number
}

const TONE_CLASSES: Record<NonNullable<ToastInput['tone']>, string> = {
  ink: 'bg-ink text-paper border-ink',
  go: 'bg-go text-white border-moss',
  tomato: 'bg-tomato text-white border-ink',
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const nextId = useRef(1)

  const push = useCallback((toast: ToastInput) => {
    const id = nextId.current++
    setToasts((prev) => [...prev.slice(-2), { ...toast, id }])
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id))
    }, toast.durationMs ?? 5000)
  }, [])

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div
        className="pointer-events-none fixed inset-x-0 bottom-20 z-50 flex flex-col items-center gap-2 px-4 md:bottom-6"
        role="status"
        aria-live="polite"
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`pointer-events-auto flex max-w-md animate-slide-up items-center gap-3 rounded-lg border px-4 py-2.5 text-sm font-semibold shadow-[3px_3px_0_0_rgba(0,0,0,0.35)] ${
              TONE_CLASSES[toast.tone ?? 'ink']
            }`}
          >
            <span>{toast.message}</span>
            {toast.action && (
              <button
                type="button"
                className="rounded border border-current px-2 py-0.5 font-mono text-xs tracking-wide uppercase transition-transform hover:scale-105"
                onClick={() => {
                  toast.action!.onClick()
                  setToasts((prev) => prev.filter((t) => t.id !== toast.id))
                }}
              >
                {toast.action.label}
              </button>
            )}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
