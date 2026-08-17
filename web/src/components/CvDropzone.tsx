import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { FileUp, Loader2 } from 'lucide-react'
import { ApiError } from '../api/client'
import { useUploadCv } from '../hooks/useCv'
import { useToast } from './Toast'

// Consent has to be given before the file leaves the browser, and it has to
// be a deliberate action rather than a pre-ticked box — a CV is special
// -category-adjacent personal data and "you uploaded it, so you agreed" is
// not consent. Stored locally so it is asked once, not on every re-upload.
const CONSENT_KEY = 'gosha.cv-consent.v1'

function hasStoredConsent(): boolean {
  try {
    return window.localStorage.getItem(CONSENT_KEY) === 'yes'
  } catch {
    return false
  }
}

function storeConsent(): void {
  try {
    window.localStorage.setItem(CONSENT_KEY, 'yes')
  } catch {
    /* private mode — the checkbox still gated this upload */
  }
}

export function CvDropzone({ onUploaded }: { onUploaded?: () => void }) {
  const toast = useToast()
  const upload = useUploadCv()
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const [consented, setConsented] = useState(hasStoredConsent)

  const handleFile = (file: File | undefined) => {
    if (!file || !consented) return
    upload.mutate(file, {
      onSuccess: () => {
        toast({ message: 'CV uploaded — your feed just got personal', tone: 'go' })
        onUploaded?.()
      },
      onError: (err) => {
        toast({
          message: err instanceof ApiError ? err.message : 'Upload failed — try again.',
          tone: 'tomato',
        })
      },
    })
  }

  return (
    <div className="space-y-3">
      <label className="flex items-start gap-2.5 text-sm leading-relaxed text-ink-soft">
        <input
          type="checkbox"
          checked={consented}
          onChange={(e) => {
            setConsented(e.target.checked)
            if (e.target.checked) storeConsent()
          }}
          className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--color-go)]"
        />
        <span>
          I agree that GOSHA may store my CV as plain text and use it to rank
          job postings for me. If I ask for a cover letter, part of my CV is
          sent to a third-party AI provider — details in the{' '}
          <Link
            to="/privacy"
            className="text-moss underline underline-offset-2 hover:text-go"
          >
            privacy notice
          </Link>
          . I can delete it at any time.
        </span>
      </label>

      <button
        type="button"
        disabled={!consented}
        aria-describedby="cv-consent-hint"
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          if (!consented) return
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          handleFile(e.dataTransfer.files[0])
        }}
        className={`flex w-full flex-col items-center gap-2 rounded-xl border-2 border-dashed p-8 transition-colors ${
          dragging ? 'border-go bg-go-soft' : 'border-rule bg-card hover:border-ink'
        } ${consented ? '' : 'cursor-not-allowed opacity-50'}`}
      >
        <input
          ref={inputRef}
          type="file"
          accept=".pdf,.docx,.txt,.md"
          className="hidden"
          onChange={(e) => handleFile(e.target.files?.[0])}
        />
        {upload.isPending ? (
          <Loader2 size={28} className="animate-spin text-go" aria-label="Uploading" />
        ) : (
          <FileUp size={28} className="text-ink-faint" aria-hidden />
        )}
        <p className="font-semibold">
          {upload.isPending ? 'Reading your CV…' : 'Drop your CV here or click to browse'}
        </p>
        <p id="cv-consent-hint" className="font-mono text-xs text-ink-faint">
          {consented
            ? 'PDF · DOCX · TXT · MD — max 5 MB'
            : 'tick the box above to enable uploads'}
        </p>
      </button>
    </div>
  )
}
