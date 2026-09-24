import type {
  Article,
  AuthPayload,
  AuthStatus,
  Candidate,
  CheckResult,
  Connection,
  EventRecord,
  Finding,
  GlobalSettings,
  Incident,
  Job,
  ListResponse,
  Measurement,
  Membership,
  Overview,
  PageRecord,
  Policy,
  Publication,
  PublicationReconciliationJob,
  Revision,
  Site,
  Team,
  User,
  WeeklyReport,
} from '../types'

const API_ROOT = '/api/v1'
let csrfToken: string | null = null

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    const message = typeof detail === 'string' ? detail : 'The request could not be completed.'
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

type RequestOptions = Omit<RequestInit, 'body'> & {
  body?: unknown
  skipCsrf?: boolean
}

function isUnsafe(method: string) {
  return !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined
  const text = await response.text()
  if (!text) return undefined
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

async function hydrateCsrf() {
  const response = await fetch(`${API_ROOT}/auth/me`, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  })
  const body = await readBody(response)
  if (response.ok && body && typeof body === 'object' && 'csrf_token' in body) {
    csrfToken = String((body as { csrf_token: string }).csrf_token)
  }
  return csrfToken
}

export function setAuthPayload(payload: Partial<AuthPayload> | null) {
  csrfToken = payload?.csrf_token ?? null
}

export function clearAuthToken() {
  csrfToken = null
}

async function request<T>(path: string, options: RequestOptions = {}, retried = false): Promise<T> {
  const method = String(options.method ?? 'GET').toUpperCase()
  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/json')
  if (options.body !== undefined) headers.set('Content-Type', 'application/json')

  if (isUnsafe(method) && !options.skipCsrf && !csrfToken) {
    await hydrateCsrf()
  }
  if (isUnsafe(method) && !options.skipCsrf && csrfToken) headers.set('X-CSRF-Token', csrfToken)

  const response = await fetch(`${API_ROOT}${path}`, {
    ...options,
    method,
    credentials: 'include',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })
  const body = await readBody(response)

  if (response.status === 403 && isUnsafe(method) && !options.skipCsrf && !retried) {
    csrfToken = null
    await hydrateCsrf()
    return request<T>(path, options, true)
  }
  if (!response.ok) {
    const detail = body && typeof body === 'object' && 'detail' in body
      ? (body as { detail: unknown }).detail
      : body ?? response.statusText
    throw new ApiError(response.status, detail)
  }
  return body as T
}

function encode(value: string) {
  return encodeURIComponent(value)
}

function asList<T>(payload: unknown): ListResponse<T> {
  if (Array.isArray(payload)) return { items: payload as T[], total: payload.length }
  const record = (payload ?? {}) as { items?: T[]; total?: number }
  return { items: record.items ?? [], total: record.total ?? record.items?.length ?? 0 }
}

async function list<T>(path: string, params: Record<string, string | number | undefined> = {}) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value))
  }
  const suffix = search.toString() ? `?${search.toString()}` : ''
  return asList<T>(await request<unknown>(`${path}${suffix}`))
}

export const authApi = {
  status: () => request<AuthStatus>('/auth/status'),
  me: async () => {
    const payload = await request<AuthPayload>('/auth/me')
    setAuthPayload(payload)
    return payload
  },
  login: async (body: { email: string; password: string }) => {
    const payload = await request<AuthPayload>('/auth/login', { method: 'POST', body, skipCsrf: true })
    setAuthPayload(payload)
    return payload
  },
  bootstrap: async (body: { email: string; password: string; name: string; team_name: string }, bootstrapToken: string) => {
    const payload = await request<AuthPayload>('/auth/bootstrap', {
      method: 'POST',
      body,
      headers: { 'X-ForgeSEO-Bootstrap-Token': bootstrapToken },
      skipCsrf: true,
    })
    setAuthPayload(payload)
    return payload
  },
  logout: async () => {
    await request('/auth/logout', { method: 'POST' })
    clearAuthToken()
  },
}

export const sitesApi = {
  list: () => list<Site>('/sites'),
  create: (body: { name: string; origin: string; timezone: string; language: string; facts: Record<string, unknown> }) =>
    request<Site>('/sites', { method: 'POST', body }),
  get: (siteId: string) => request<Site>(`/sites/${encode(siteId)}`),
  update: (siteId: string, body: Record<string, unknown>) => request<Site>(`/sites/${encode(siteId)}`, { method: 'PATCH', body }),
  overview: (siteId: string) => request<Overview>(`/sites/${encode(siteId)}/overview`),
}

export const connectionsApi = {
  list: (siteId: string) => list<Connection>(`/sites/${encode(siteId)}/connections`),
  save: (siteId: string, kind: string, body: { credentials: Record<string, unknown>; settings: Record<string, unknown> }) =>
    request<Connection>(`/sites/${encode(siteId)}/connections/${encode(kind)}`, { method: 'PUT', body }),
  oauthStartUrl: (siteId: string, kind: 'gsc' | 'ga4') =>
    `${API_ROOT}/sites/${encode(siteId)}/connections/${encode(kind)}/oauth/start`,
  test: (siteId: string, kind: string) => request<Job>(`/sites/${encode(siteId)}/connections/${encode(kind)}/test`, { method: 'POST', body: {} }),
  revoke: (siteId: string, kind: string) => request<void>(`/sites/${encode(siteId)}/connections/${encode(kind)}`, { method: 'DELETE' }),
}

export const policyApi = {
  get: (siteId: string) => request<Policy>(`/sites/${encode(siteId)}/policy`),
  update: (siteId: string, settings: Record<string, unknown>) => request<Policy>(`/sites/${encode(siteId)}/policy`, { method: 'PUT', body: { settings } }),
}

export const pagesApi = {
  list: (siteId: string, params?: Record<string, string | number | undefined>) => list<PageRecord>(`/sites/${encode(siteId)}/pages`, params),
  enroll: (siteId: string, pageId: string, enrolled: boolean) => request<PageRecord>(`/sites/${encode(siteId)}/pages/${encode(pageId)}`, { method: 'PATCH', body: { enrolled } }),
  findings: (siteId: string, params?: Record<string, string | number | undefined>) => list<Finding>(`/sites/${encode(siteId)}/findings`, params),
  candidates: (siteId: string, params?: Record<string, string | number | undefined>) => list<Candidate>(`/sites/${encode(siteId)}/candidates`, params),
  decide: (siteId: string, candidateId: string, decision: 'approve' | 'reject') =>
    request<Candidate>(`/sites/${encode(siteId)}/candidates/${encode(candidateId)}/decision`, { method: 'POST', body: { decision } }),
  execute: (siteId: string, candidateId: string) => request<Job>(`/sites/${encode(siteId)}/candidates/${encode(candidateId)}/execute`, { method: 'POST', body: {} }),
}

export const jobsApi = {
  create: (siteId: string, body: { kind: string; payload: Record<string, unknown>; idempotency_key?: string }) =>
    request<Job>(`/sites/${encode(siteId)}/jobs`, { method: 'POST', body }),
  list: (siteId: string, params?: Record<string, string | number | undefined>) => list<Job>(`/sites/${encode(siteId)}/jobs`, params),
  get: <T extends Job = Job>(siteId: string, jobId: string) => request<T>(`/sites/${encode(siteId)}/jobs/${encode(jobId)}`),
  wait: async <T extends Job = Job>(siteId: string, jobId: string, options: { timeoutMs?: number; intervalMs?: number; onUpdate?: (job: T) => void } = {}): Promise<T> => {
    const timeoutMs = Math.max(0, options.timeoutMs ?? 20_000)
    const intervalMs = Math.max(250, options.intervalMs ?? 1_000)
    const terminal = new Set(['complete', 'partial', 'failed', 'blocked', 'needs_reconciliation', 'ambiguous', 'rolled_back'])
    const held = (candidate: T) => candidate.status === 'queued' && candidate.result?.status === 'held'
    const started = Date.now()
    let job = await jobsApi.get<T>(siteId, jobId)
    options.onUpdate?.(job)
    while (!terminal.has(job.status) && !held(job) && Date.now() - started < timeoutMs) {
      await new Promise((resolve) => window.setTimeout(resolve, intervalMs))
      job = await jobsApi.get<T>(siteId, jobId)
      options.onUpdate?.(job)
    }
    return job
  },
}

export const articlesApi = {
  list: (siteId: string, params?: Record<string, string | number | undefined>) => list<Article>(`/sites/${encode(siteId)}/articles`, params),
  create: (siteId: string, body: { title: string; brief: Record<string, unknown>; sources: unknown[]; author_id?: string }) =>
    request<Article>(`/sites/${encode(siteId)}/articles`, { method: 'POST', body }),
  get: (siteId: string, articleId: string) => request<Article>(`/sites/${encode(siteId)}/articles/${encode(articleId)}`),
  update: (siteId: string, articleId: string, body: Record<string, unknown>) => request<Article>(`/sites/${encode(siteId)}/articles/${encode(articleId)}`, { method: 'PATCH', body }),
  revisions: (siteId: string, articleId: string) => list<Revision>(`/sites/${encode(siteId)}/articles/${encode(articleId)}/revisions`),
  check: (siteId: string, articleId: string) => request<CheckResult>(`/sites/${encode(siteId)}/articles/${encode(articleId)}/check`, { method: 'POST', body: {} }),
  reviewSource: (siteId: string, articleId: string, body: { url: string; notes: string; expected_updated_at: string; confirms_claim_support: boolean }) =>
    request<Article>(`/sites/${encode(siteId)}/articles/${encode(articleId)}/source-reviews`, { method: 'POST', body }),
  schedule: (siteId: string, articleId: string, scheduled_at: string) => request<Article>(`/sites/${encode(siteId)}/articles/${encode(articleId)}/schedule`, { method: 'POST', body: { scheduled_at } }),
  publish: (siteId: string, articleId: string) => request<Job>(`/sites/${encode(siteId)}/articles/${encode(articleId)}/publish`, { method: 'POST', body: {} }),
  rollback: (siteId: string, articleId: string) => request<Job>(`/sites/${encode(siteId)}/articles/${encode(articleId)}/rollback`, { method: 'POST', body: {} }),
}

export const storeApi = {
  products: (siteId: string, params?: Record<string, string | number | undefined>) => list<PageRecord>(`/sites/${encode(siteId)}/products`, params),
}

export const operationsApi = {
  incidents: (siteId: string, params?: Record<string, string | number | undefined>) => list<Incident>(`/sites/${encode(siteId)}/incidents`, params),
  activity: (siteId: string, params?: Record<string, string | number | undefined>) => list<EventRecord>(`/sites/${encode(siteId)}/activity`, params),
  measurements: (siteId: string, params?: Record<string, string | number | undefined>) => list<Measurement>(`/sites/${encode(siteId)}/measurements`, params),
  publications: (siteId: string, params?: Record<string, string | number | undefined>) => list<Publication>(`/sites/${encode(siteId)}/publications`, params),
  importMeasurements: (siteId: string, items: unknown[]) => request<{ imported: number }>(`/sites/${encode(siteId)}/measurements/import`, { method: 'POST', body: { items } }),
  weekly: (siteId: string) => request<WeeklyReport>(`/sites/${encode(siteId)}/reports/weekly`),
  weeklyCsv: async (siteId: string) => {
    const response = await fetch(`${API_ROOT}/sites/${encode(siteId)}/reports/weekly?format=csv`, { credentials: 'include', headers: { Accept: 'text/csv' } })
    if (!response.ok) throw new ApiError(response.status, response.statusText)
    return response.blob()
  },
}

export const publicationsApi = {
  reconcile: (siteId: string, publicationId: string) =>
    request<PublicationReconciliationJob>(`/sites/${encode(siteId)}/publications/${encode(publicationId)}/reconcile`, { method: 'POST', body: {} }),
}

export const teamApi = {
  get: async () => {
    const payload = await request<unknown>('/team')
    if (Array.isArray(payload)) return { items: payload as Membership[], total: payload.length }
    const record = payload as { items?: Membership[]; total?: number; team?: Team; members?: Membership[] }
    return { items: record.items ?? record.members ?? [], total: record.total ?? record.items?.length ?? record.members?.length ?? 0, team: record.team }
  },
  addMember: (body: { email: string; name: string; password: string; role: 'owner' | 'editor' | 'viewer' }) =>
    request<Membership>('/team/members', { method: 'POST', body }),
  updateMember: (userId: string, role: 'owner' | 'editor' | 'viewer') =>
    request<{ member: Membership }>(`/team/members/${encode(userId)}`, { method: 'PATCH', body: { role } }),
  removeMember: (userId: string) =>
    request<{ ok: boolean; user_id: string }>(`/team/members/${encode(userId)}`, { method: 'DELETE' }),
}

export const settingsApi = {
  get: () => request<GlobalSettings>('/settings'),
  update: (body: Record<string, unknown>) => request<GlobalSettings>('/settings', { method: 'PATCH', body }),
}

export interface CostReservationRecord {
  id:string; status:string; estimated_cents:number; actual_cents:number|null; operation_key:string
}
export const budgetsApi = {
  get:(siteId:string)=>request<{reservations:ListResponse<CostReservationRecord>}>(`/sites/${encode(siteId)}/budgets`),
  settle:(siteId:string,id:string,actual_cents:number,evidence:string)=>request<CostReservationRecord>(`/sites/${encode(siteId)}/budgets/reservations/${encode(id)}/settle`,{method:'POST',body:{actual_cents,evidence}}),
}

export function detailMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (typeof error.detail === 'string') return error.detail
    if (error.detail && typeof error.detail === 'object') {
      const detail = error.detail as { message?: string; error?: string }
      return detail.message ?? detail.error ?? error.message
    }
    return error.message
  }
  if (error instanceof Error) return error.message
  return 'Something went wrong. Please try again.'
}

export type AuthContextValue = {
  user: User | null
  team: Team | null
  role: AuthPayload['role'] | null
  status: 'loading' | 'authenticated' | 'unauthenticated' | 'uninitialized'
  signIn: (email: string, password: string) => Promise<AuthPayload>
  bootstrap: (body: { email: string; password: string; name: string; team_name: string }, bootstrapToken: string) => Promise<AuthPayload>
  signOut: () => Promise<void>
  refresh: () => Promise<AuthPayload | null>
}
