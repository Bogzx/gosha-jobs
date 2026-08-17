// Mirrors the /api/v1 contracts (gosha/api/schemas.py)

export interface Me {
  id: number
  discord_id: string
  username: string | null
  avatar_url: string | null
  tier: string
  in_guild: boolean
  has_cv: boolean
  is_admin: boolean
}

export interface Job {
  id: number
  url: string
  title: string
  company: string
  location: string
  description: string | null
  salary_min: number | null
  salary_max: number | null
  salary_currency: string | null
  source: string
  posted_at: string | null
  first_seen_at: string | null
  /** Raw cosine similarity. Do not render it — see match_percentile. */
  match_score: number | null
  /** 0-100 position within the whole ranked candidate set. */
  match_percentile: number | null
  match_reasons: string[] | null
  feedback: 'interested' | 'not_relevant' | null
  applied: boolean
}

export interface JobList {
  items: Job[]
  total: number
  page: number
  per_page: number
}

export interface JobSummary {
  id: number
  title: string
  company: string
  location: string
  url: string
  source: string
}

export type ApplicationStatus =
  | 'applied'
  | 'phone_screen'
  | 'interview'
  | 'offer'
  | 'rejected'
  | 'withdrawn'

export interface Application {
  id: number
  job_id: number
  status: ApplicationStatus
  notes: string | null
  applied_at: string | null
  updated_at: string | null
  source: string
  job: JobSummary
}

export interface Subscription {
  id: number
  name: string | null
  keywords: string[]
  locations: string[]
  excluded_keywords: string[]
  company_blacklist: string[]
  experience_levels: string[]
  remote_ok: boolean
  salary_min: number | null
  max_age_days: number
  is_active: boolean
  notify_discord: boolean
  created_at: string | null
}

export interface SubscriptionInput {
  name?: string | null
  keywords: string[]
  locations: string[]
  excluded_keywords?: string[]
  company_blacklist?: string[]
  experience_levels?: string[]
  remote_ok?: boolean
  salary_min?: number | null
  max_age_days?: number
  notify_discord?: boolean
}

export interface CvInfo {
  has_cv: boolean
  text: string | null
  uploaded_at: string | null
}

export interface CoverLetter {
  id: number
  job_id: number
  job_title: string
  company: string
  content: string
  created_at: string | null
}

export interface AdminStats {
  total_users: number
  total_jobs: number
  active_jobs: number
  total_subscriptions: number
  active_subscriptions: number
  total_deliveries: number
  total_feedback: number
  total_applications: number
}

export interface UsageDay {
  date: string
  pageviews: number
  active_users: number
  signups: number
  applies: number
}

export interface ScrapeHealth {
  jobs_by_source: Record<string, number>
  last_job_discovered_at: string | null
  recent_events: { event_type: string; timestamp: string | null; job_id: number | null }[]
}

export interface JobFilters {
  q?: string
  locations?: string
  experience?: string
  sources?: string
  salary_min?: number
  posted_within_days?: number
  remote?: boolean
}
