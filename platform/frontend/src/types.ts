export type Id = string

export type Role = 'owner' | 'editor' | 'viewer'
export type Severity = 'critical' | 'high' | 'medium' | 'low' | string
export type Status = string

export interface BusinessFacts {
  business_name?: string
  audience?: string
  locations?: string[]
  services?: string[]
  products?: string[]
  authors?: Array<{ id?: string; name: string; email?: string } | string>
  confirmed_sources?: Array<string | { url?: string; title?: string; [key: string]: unknown }>
  language?: string
  brand_tone?: string
  [key: string]: unknown
}

export interface User {
  id: Id
  email: string
  name: string
}

export interface Team {
  id: Id
  name: string
  created_at?: string
}

export interface AuthPayload {
  user: User
  team: Team
  role: Role
  csrf_token: string
}

export interface AuthStatus {
  initialized: boolean
}

export interface Site {
  id: Id
  team_id?: Id
  name: string
  origin: string
  timezone: string
  language: string
  facts: BusinessFacts
  paused: boolean
  created_at?: string
}

export interface Connection {
  id?: Id
  site_id?: Id
  kind: string
  status: 'connected' | 'needs_connection' | 'error' | 'testing' | string
  capabilities?: Record<string, unknown>
  checked_at?: string | null
  created_at?: string
  safe_fields?: Record<string, string>
  settings?: Record<string, unknown>
  credentials?: Record<string, unknown>
  error?: string
}

export interface Overview {
  site: Site
  counts: {
    pages: number
    open_findings: number
    pending_candidates: number
    published_articles: number
    open_incidents: number
  }
  monitoring: {
    status: string
    last_seen_at?: string | null
    queue_delay_seconds?: number | null
    missed_checks?: number | null
    due_jobs?: number | null
    wordpress_change_poll?: {
      status: string
      last_success_at?: string | null
      seconds_since_success?: number | null
      seconds_since_verification?: number | null
      missed_window?: boolean
      message?: string
    }
  }
  budget: { limit_cents: number; spent_cents: number; reserved_cents: number }
  global_pause?: boolean
  policy?: {
    enabled?: boolean
    allowed_actions?: string[]
    status?: string
    settings?: {
      enabled?: boolean
      allowed_actions?: string[]
      [key: string]: unknown
    }
    [key: string]: unknown
  } | null
  policy_state?: {
    enabled?: boolean
    allowed_actions?: string[]
    status?: string
    [key: string]: unknown
  } | null
  recent_events: EventRecord[]
  coverage: { status: string; last_audit_at?: string | null; error_count?: number; pending_url_count?: number }
  connections: Connection[]
}

export interface PageRecord {
  id: Id
  site_id?: Id
  resource_key: string
  url: string
  title?: string
  resource_type?: string
  source?: Record<string, unknown>
  signals?: Record<string, unknown>
  source_hash?: string
  enrolled: boolean
  managed: boolean
  last_seen_at?: string | null
  created_at?: string
}

export interface Finding {
  id: Id
  page_id?: Id | null
  key: string
  code: string
  severity: Severity
  title: string
  details?: Record<string, unknown>
  status: string
  first_seen_at?: string
  last_seen_at?: string
  resolved_at?: string | null
  recurrence_count?: number
}

export interface Candidate {
  id: Id
  page_id: Id
  field: string
  before_value: string
  after_value: string
  source_hash: string
  status: string
  policy_version?: number | null
  details?: Record<string, unknown>
  created_at?: string
  page?: PageRecord
}

export interface Article {
  id: Id
  site_id?: Id
  title: string
  slug?: string
  body: string
  status: 'planned' | 'drafting' | 'checking' | 'checked' | 'review_needed' | 'scheduled' | 'publishing' | 'verifying' | 'published' | 'failed' | 'rolled_back' | string
  brief?: Record<string, unknown>
  checks?: CheckResult | null
  sources: Array<Record<string, unknown> | string>
  author_id?: string | null
  scheduled_at?: string | null
  remote_id?: string | null
  managed: boolean
  created_at?: string
  updated_at?: string
}

export interface Revision {
  id: Id
  article_id?: Id
  body: string
  title: string
  reason?: string
  created_at: string
}

export interface FullCycleStage {
  name: string
  status: string
  reason?: string
  result?: Record<string, unknown>
  // Returned for server reconciliation, but intentionally never shown in the UI.
  stage_job_id?: Id
}

export interface FullCycleNextAction {
  action: string
  status: string
  reason?: string
  stages?: string[]
}

export interface FullCycleExecutionGate {
  status: string
  reason?: string
}

export interface FullCycleExecutionSummary {
  metadata?: {
    candidates_authorized?: number
    jobs_queued?: number
  }
  metadata_candidates_authorized?: number
  metadata_jobs_queued?: number
  content_publishing?: FullCycleExecutionGate
  paid_visibility?: FullCycleExecutionGate
  remote_mutations?: FullCycleExecutionGate
}

export interface ContentAutopilotStage {
  name: string
  status: string
  reason?: string
  message?: string
}

export interface ContentAutopilotResult {
  workflow: 'content_autopilot' | string
  status?: 'gated' | 'needs_review' | 'published' | 'failed' | 'ambiguous' | string
  policy_version?: number | null
  article_status?: string | null
  article?: { status?: string | null }
  stages?: ContentAutopilotStage[]
  stage_records?: ContentAutopilotStage[]
  blockers?: unknown[]
  next_action?: unknown
  next_actions?: unknown[]
  [key: string]: unknown
}

export interface FullCycleResult {
  workflow: 'full_cycle'
  mode?: string
  complete?: boolean
  stages: FullCycleStage[]
  next_actions: FullCycleNextAction[]
  execution_summary?: FullCycleExecutionSummary
}

export interface Job {
  id: Id
  site_id?: Id
  kind: string
  status: string
  payload?: Record<string, unknown>
  result?: Record<string, unknown>
  idempotency_key?: string
  attempts?: number
  available_at?: string
  created_at?: string
  updated_at?: string
}

export type PublicationReconciliationStatus = 'published' | 'draft_reconciled' | 'held' | 'already_resolved' | string

export interface PublicationReconciliationResult {
  workflow: 'reconcile_publication' | string
  status: PublicationReconciliationStatus
  complete?: boolean
  reason?: string
  next_action?: string
  // The API may add safe bookkeeping fields. Consumers must choose an allowlist
  // before displaying any result value rather than rendering this object raw.
  [key: string]: unknown
}

export type PublicationReconciliationJob = Omit<Job, 'result'> & {
  result?: PublicationReconciliationResult
}

export type JobProgressKind = 'audit' | 'inventory' | 'plan' | 'generate' | 'publish' | 'availability' | 'visibility' | 'refresh' | 'full_cycle' | 'other'
export type JobProgressStatus = 'queued' | 'running' | 'complete' | 'partial' | 'failed' | 'blocked' | 'cancelled' | 'retrying' | 'needs_connection' | 'needs_review' | 'unknown'
export type JobProgressStage = 'availability' | 'inventory' | 'public_audit' | 'content_plan' | 'refresh_evaluation' | 'audit' | 'plan' | 'generate' | 'publish' | 'visibility' | 'refresh'

/**
 * The deliberately small, display-safe subset of a job progress event.
 * job_id is retained only for event correlation; it must never be rendered.
 */
export interface JobProgress {
  site_id?: Id | null
  job_id: Id
  kind: JobProgressKind
  status: JobProgressStatus
  stage?: JobProgressStage
  completed?: number
  total?: number
  stage_index?: number
  stage_count?: number
  percent?: number
  updated_at?: string
}

export interface Incident {
  id: Id
  key: string
  kind: string
  severity: Severity
  title: string
  status: string
  details?: Record<string, unknown>
  failure_count?: number
  first_seen_at?: string
  last_seen_at?: string
  resolved_at?: string | null
}

export interface EventRecord {
  id: number | string
  site_id?: Id | null
  team_id?: Id
  kind: string
  message: string
  data?: Record<string, unknown>
  created_at?: string
}

export interface Measurement {
  id?: Id
  kind: string
  source: string
  data: Record<string, unknown>
  observed_at: string
  created_at?: string
}

export interface Publication {
  id: Id
  article_id?: Id | null
  candidate_id?: Id | null
  operation_key: string
  status: string
  policy_version: number
  snapshot?: Record<string, unknown>
  result?: Record<string, unknown>
  remote_id?: string | null
  created_at?: string
  updated_at?: string
}

export interface Policy {
  id: Id
  version: number
  settings: PolicySettings
  created_at?: string
}

export interface PolicySettings {
  enabled: boolean
  allowed_actions: string[]
  protected_paths: string[]
  posts_per_week: number
  refreshes_per_week: number
  monthly_budget_cents: number
  tracked_keywords: string[]
  competitors: string[]
  tracked_questions: string[]
  publish_days: number[]
  author_id?: string | null
  [key: string]: unknown
}

export interface Membership {
  id?: Id
  user_id?: Id
  email: string
  name: string
  role: Role
  created_at?: string
}

export interface GlobalSettings {
  global_pause: boolean
  [key: string]: unknown
}

export interface WeeklyReport {
  [key: string]: unknown
}

export interface ListResponse<T> {
  items: T[]
  total: number
}

export interface CheckResult {
  passed: boolean
  blockers: string[]
  warnings: string[]
  [key: string]: unknown
}

export interface ApiErrorShape {
  detail: string | Record<string, unknown>
}
