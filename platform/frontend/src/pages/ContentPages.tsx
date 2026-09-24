import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { AlertCircle, ArrowLeft, CalendarDays, Check, CheckCircle2, Clock3, FileText, History, Plus, RefreshCw, RotateCcw, Save, Send, ShieldAlert, Sparkles, XCircle } from 'lucide-react'
import { Badge, Button, EmptyState, ErrorState, Field, Notice, PageHeader, Panel, TableShell } from '../components/ui'
import { articlesApi, connectionsApi, detailMessage, jobsApi, pagesApi, policyApi, settingsApi, sitesApi } from '../lib/api'
import { formatDate, formatDateTime, fromDateTimeLocal, titleCase, toDateTimeLocal, truncate } from '../lib/format'
import type { Article, CheckResult, Connection, ContentAutopilotResult, GlobalSettings, PageRecord, Policy, Revision, Site } from '../types'
import { ResourceStateView, useResource, useSiteId } from './shared'
import { useAuth } from '../context/AppContext'
import { ProviderUsagePanel } from '../components/ProviderUsagePanel'

const IMAGE_SOURCE_KINDS = ['owner_provided', 'licensed', 'generated_illustration'] as const
type ImageSourceKind = typeof IMAGE_SOURCE_KINDS[number]
type ImageSourceRecord = {
  url: string
  kind: ImageSourceKind
  attribution?: string
  license?: string
  alt?: string
  disclosure?: string
  owner_confirmed?: boolean
  not_real?: boolean
}
const MAX_IMAGE_SOURCES = 12

function isImageSourceKind(value: unknown): value is ImageSourceKind {
  return typeof value === 'string' && IMAGE_SOURCE_KINDS.includes(value as ImageSourceKind)
}

function normalizeImageSources(value: unknown): ImageSourceRecord[] {
  if (!Array.isArray(value)) return []
  return value.map((item): ImageSourceRecord | null => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null
    const record = item as Record<string, unknown>
    const url = typeof record.url === 'string' ? record.url.trim() : ''
    if (!url || !isImageSourceKind(record.kind)) return null
    const normalized: ImageSourceRecord = { url, kind: record.kind }
    for (const field of ['attribution', 'license', 'alt'] as const) {
      if (typeof record[field] === 'string' && record[field].trim()) normalized[field] = record[field].trim()
    }
    if (record.kind === 'generated_illustration' && typeof record.disclosure === 'string' && record.disclosure.trim()) normalized.disclosure = record.disclosure.trim()
    if (record.kind === 'owner_provided') normalized.owner_confirmed = record.owner_confirmed === true
    if (record.kind === 'generated_illustration') normalized.not_real = record.not_real === true
    return normalized
  }).filter((item): item is ImageSourceRecord => item !== null).slice(0, MAX_IMAGE_SOURCES)
}

function imageSourcesPayload(records: ImageSourceRecord[]) {
  return records.map((record) => {
    const payload: Record<string, unknown> = { url: record.url.trim(), kind: record.kind }
    for (const field of ['attribution', 'license', 'alt'] as const) {
      const value = record[field]?.trim()
      if (value) payload[field] = value
    }
    if (record.kind === 'generated_illustration' && record.disclosure?.trim()) payload.disclosure = record.disclosure.trim()
    if (record.kind === 'owner_provided') payload.owner_confirmed = true
    if (record.kind === 'generated_illustration') payload.not_real = true
    return payload
  })
}

function imageSourceValidationError(records: ImageSourceRecord[]) {
  if (records.length > MAX_IMAGE_SOURCES) return `Keep image provenance to ${MAX_IMAGE_SOURCES} records or fewer.`
  for (const [index, record] of records.entries()) {
    if (!record.url.trim()) return `Add a URL for image ${index + 1}, or remove the empty record.`
    try {
      const url = new URL(record.url.trim())
      if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) throw new Error('invalid')
    } catch {
      return `Image ${index + 1} needs a public http:// or https:// URL.`
    }
    if (record.kind === 'owner_provided' && record.owner_confirmed !== true) return `Confirm that image ${index + 1} was provided by the site owner.`
    if (record.kind === 'licensed' && !record.attribution?.trim()) return `Add attribution for licensed image ${index + 1}.`
    if (record.kind === 'licensed' && !record.license?.trim()) return `Add the license for licensed image ${index + 1}.`
    if (record.kind === 'generated_illustration' && !record.disclosure?.trim()) return `Add a disclosure for generated image ${index + 1}.`
    if (record.kind === 'generated_illustration' && record.not_real !== true) return `Confirm that generated image ${index + 1} is not a real product, premises, or completed work.`
  }
  return null
}

function imageSourceKindLabel(kind: ImageSourceKind) {
  if (kind === 'owner_provided') return 'Owner-provided'
  if (kind === 'generated_illustration') return 'Generated illustration'
  return 'Licensed'
}

function weekDays() {
  const today = new Date()
  const monday = new Date(today)
  const day = monday.getDay()
  const offset = day === 0 ? -6 : 1 - day
  monday.setDate(monday.getDate() + offset)
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(monday)
    date.setDate(monday.getDate() + index)
    return date
  })
}

function plannedWeek(article: Article) {
  const raw = article.brief?.week
  const value = typeof raw === 'number' ? raw : Number(raw)
  return Number.isInteger(value) && value >= 1 && value <= 4 ? value : null
}

type CalendarReadinessTone = 'teal' | 'amber' | 'red' | 'slate' | 'blue' | 'green'

interface CalendarReadiness {
  label: string
  tone: CalendarReadinessTone
  description: string
}

function calendarReadiness(connection?: Connection): CalendarReadiness {
  const status = connection?.status?.toLowerCase() ?? ''
  const lastTest = connection?.capabilities?.last_connection_test
  const testStatus = lastTest && typeof lastTest === 'object' && !Array.isArray(lastTest)
    ? String((lastTest as Record<string, unknown>).status ?? '').toLowerCase()
    : ''

  if (!connection || !status || ['needs_connection', 'revoked', 'disconnected'].includes(status)) {
    return { label: 'Needs connection', tone: 'amber', description: 'Connect this source before ForgeSEO can use it for this workflow.' }
  }
  if (status === 'error' || testStatus === 'error') {
    return { label: 'Error', tone: 'red', description: 'The last access check failed. Review the connection before using this source.' }
  }
  if (status === 'connected') {
    if (!connection.checked_at) {
      return { label: 'Needs review', tone: 'amber', description: 'Access details exist, but no completed connection check is recorded yet.' }
    }
    return { label: 'Connected', tone: 'green', description: 'Access was checked. Policy and editorial checks still control any publication or change.' }
  }
  if (status === 'unsupported') {
    return { label: 'Unsupported', tone: 'red', description: 'This source cannot provide the capability required by this workflow.' }
  }
  return { label: 'Needs review', tone: 'amber', description: 'The connection is present, but its latest access state needs review.' }
}

function CalendarConnectionCard({ id, title, connection, readiness, description }: { id: string; title: string; connection?: Connection; readiness: CalendarReadiness; description: string }) {
  return <section aria-labelledby={id} style={{ border: '1px solid var(--line)', borderRadius: 10, padding: 14, background: 'var(--surface-muted)' }}>
    <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12 }}>
      <h3 id={id} className="panel-title" style={{ margin: 0, fontSize: '1rem' }}>{title}</h3>
      <Badge value={readiness.label} tone={readiness.tone} />
    </div>
    <p className="text-small text-muted" style={{ margin: '8px 0' }}>{readiness.description}</p>
    <p className="text-small text-muted" style={{ margin: 0 }}>{description}</p>
    <div className="text-small text-muted" style={{ marginTop: 10 }}>Connection checked: {formatDateTime(connection?.checked_at, 'Not yet checked')}</div>
  </section>
}

type CalendarReadResult<T> = { value: T | null; error: string | null }
type AutopilotCheckState = 'ready' | 'needs_connection' | 'needs_review' | 'paused' | 'error' | 'unsupported'

interface CalendarReadError {
  key: string
  message: string
}

interface AutopilotCheck {
  key: string
  label: string
  state: AutopilotCheckState
  description: string
}

interface ContentAutopilotOutcome {
  kind: 'published' | 'gated' | 'needs_review' | 'failed' | 'ambiguous' | 'running'
  title: string
  message: string
  articleStatus?: string | null
  blockers: string[]
  nextAction?: string | null
}

interface CalendarData extends Awaited<ReturnType<typeof articlesApi.list>> {
  connections: Connection[]
  site: Site | null
  policy: Policy | null
  settings: GlobalSettings | null
  pages: PageRecord[]
  readErrors: CalendarReadError[]
  refreshedAt: string
}

async function readCalendarSource<T>(reader: () => Promise<T>): Promise<CalendarReadResult<T>> {
  try {
    return { value: await reader(), error: null }
  } catch (requestError) {
    return { value: null, error: detailMessage(requestError) }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

function isSite(value: unknown): value is Site {
  return isRecord(value)
    && typeof value.id === 'string'
    && typeof value.name === 'string'
    && typeof value.origin === 'string'
    && typeof value.timezone === 'string'
    && typeof value.language === 'string'
    && typeof value.paused === 'boolean'
}

function isPolicy(value: unknown): value is Policy {
  return isRecord(value)
    && typeof value.id === 'string'
    && typeof value.version === 'number'
    && isRecord(value.settings)
    && typeof value.settings.enabled === 'boolean'
    && Array.isArray(value.settings.allowed_actions)
}

function isGlobalSettings(value: unknown): value is GlobalSettings {
  return isRecord(value) && typeof value.global_pause === 'boolean'
}

function sourceError(key: string, result: CalendarReadResult<unknown>, fallback: string): CalendarReadError | null {
  if (result.value !== null) return null
  return { key, message: result.error || fallback }
}

function checkedConnection(connection: Connection | undefined, label: string, readError?: CalendarReadError, requirement?: 'wordpress' | 'ai'): AutopilotCheck {
  if (readError) return { key: label.toLowerCase(), label, state: 'needs_review', description: `${label} could not be verified. Refresh this page and review the connection before running content autopilot.` }
  if (!connection || !connection.status) return { key: label.toLowerCase(), label, state: 'needs_connection', description: `Connect and test ${label} before ForgeSEO can generate or publish an article.` }
  const status = connection.status.toLowerCase()
  const testStatus = isRecord(connection.capabilities?.last_connection_test)
    ? String(connection.capabilities.last_connection_test.status ?? '').toLowerCase()
    : ''
  if (['needs_connection', 'revoked', 'disconnected'].includes(status)) return { key: label.toLowerCase(), label, state: 'needs_connection', description: `Connect and test ${label} before ForgeSEO can generate or publish an article.` }
  if (status === 'error' || testStatus === 'error') return { key: label.toLowerCase(), label, state: 'error', description: `The latest ${label} access check failed. Review the connection before trying again.` }
  if (status === 'stale' || testStatus === 'stale') return { key: label.toLowerCase(), label, state: 'needs_review', description: `The latest ${label} verification is stale. Run a fresh connection check before trying again.` }
  if (status === 'unsupported') return { key: label.toLowerCase(), label, state: 'unsupported', description: `${label} does not currently provide the capability required by this workflow.` }
  if (status !== 'connected' || !connection.checked_at || !connection.capabilities || !Object.keys(connection.capabilities).length) {
    return { key: label.toLowerCase(), label, state: 'needs_review', description: `${label} is present, but a completed capability check is not available.` }
  }
  if (requirement === 'wordpress') {
    const native = isRecord(connection.capabilities.native) ? connection.capabilities.native : null
    if (connection.capabilities.authenticated !== true || native?.create !== true || native.publish !== true) {
      return { key: label.toLowerCase(), label, state: 'needs_review', description: `${label} is connected, but verified post creation and publication capabilities are not recorded.` }
    }
  }
  if (requirement === 'ai') {
    const settings = isRecord(connection.capabilities.settings) ? connection.capabilities.settings : null
    const endpoint = settings && typeof settings.endpoint === 'string' && settings.endpoint.trim()
    const model = settings && typeof settings.model === 'string' && settings.model.trim()
    const estimate = settings?.estimated_cost_cents
    const maximum = settings?.max_cost_cents
    const pricingReady = typeof estimate === 'number' && Number.isInteger(estimate) && estimate >= 0
      && typeof maximum === 'number' && Number.isInteger(maximum) && maximum > 0 && estimate <= maximum
    if (!endpoint || !model || !pricingReady) {
      return { key: label.toLowerCase(), label, state: 'needs_review', description: `${label} is connected, but its endpoint, model, and verified request pricing are not all recorded.` }
    }
  }
  return { key: label.toLowerCase(), label, state: 'ready', description: `${label} access and capabilities were checked. The server will re-check them before any write.` }
}

function verifiedAuthorIds(data: CalendarData) {
  const ids = new Set<string>()
  const wordpress = data.connections.find((connection) => connection.kind === 'wordpress')
  const authenticated = wordpress?.capabilities?.authenticated_author
  if (isRecord(authenticated) && authenticated.id !== undefined && String(authenticated.id).trim()) ids.add(String(authenticated.id))
  for (const page of data.pages.filter((item) => item.resource_type === 'authors')) {
    const id = page.resource_key.split(':').slice(1).join(':')
    if (id) ids.add(id)
  }
  return ids
}

function contentAutopilotChecks(data: CalendarData, role: ReturnType<typeof useAuth>['role'], stale: boolean, resourceError: string | null): AutopilotCheck[] {
  const checks: AutopilotCheck[] = []
  checks.push(role === 'viewer'
    ? { key: 'role', label: 'Your role', state: 'needs_review', description: 'Viewer access can review the calendar, but cannot start content autopilot.' }
    : role === 'owner' || role === 'editor'
      ? { key: 'role', label: 'Your role', state: 'ready', description: 'Your role can request this bounded workflow. The API remains authoritative.' }
      : { key: 'role', label: 'Your role', state: 'needs_review', description: 'Your workspace role is not available yet. Refresh before trying again.' })

  if (stale || resourceError) checks.push({ key: 'freshness', label: 'Calendar state', state: 'needs_review', description: 'The latest calendar or readiness response is stale. Refresh before starting a new workflow.' })

  const siteError = data.readErrors.find((item) => item.key === 'site')
  checks.push(!data.site || siteError
    ? { key: 'site', label: 'Site state', state: 'needs_review', description: 'Site pause state could not be verified. Refresh and review Settings before trying again.' }
    : data.site.paused
      ? { key: 'site', label: 'Site pause', state: 'paused', description: 'This site is paused. Resume it in Settings only after the pilot safeguards are ready.' }
      : { key: 'site', label: 'Site pause', state: 'ready', description: 'The site pause is off. The policy and connection checks below still apply.' })

  const settingsError = data.readErrors.find((item) => item.key === 'settings')
  checks.push(!data.settings || settingsError
    ? { key: 'workspace', label: 'Workspace pause', state: 'needs_review', description: 'The workspace emergency-pause state could not be verified.' }
    : data.settings.global_pause
      ? { key: 'workspace', label: 'Workspace pause', state: 'paused', description: 'The workspace emergency pause is active. Content autopilot is held.' }
      : { key: 'workspace', label: 'Workspace pause', state: 'ready', description: 'The workspace emergency pause is off. Site policy still controls this action.' })

  const policyError = data.readErrors.find((item) => item.key === 'policy')
  if (!data.policy || policyError) {
    checks.push({ key: 'policy', label: 'Publishing policy', state: 'needs_review', description: 'The current policy could not be verified. Review policy settings before trying again.' })
  } else if (!data.policy.settings.enabled) {
    checks.push({ key: 'policy', label: 'Publishing policy', state: 'needs_review', description: 'The site policy is disabled. An owner must enable the policy before this workflow can run.' })
  } else if (!data.policy.settings.allowed_actions.includes('publish')) {
    checks.push({ key: 'policy', label: 'Publishing policy', state: 'needs_review', description: 'The policy does not allow publishing. Add the publish action only after reviewing the safeguards.' })
  } else {
    checks.push({ key: 'policy', label: 'Publishing policy', state: 'ready', description: 'The current enabled policy allows publishing. The server will evaluate the policy version again at execution time.' })
  }

  const connectionsError = data.readErrors.find((item) => item.key === 'connections')
  const wordpress = data.connections.find((connection) => connection.kind === 'wordpress')
  const ai = data.connections.find((connection) => connection.kind === 'ai')
  checks.push(checkedConnection(wordpress, 'WordPress connection', connectionsError, 'wordpress'))
  checks.push(checkedConnection(ai, 'AI connection', connectionsError, 'ai'))

  const authorId = data.policy?.settings.author_id
  const authorIds = verifiedAuthorIds(data)
  const authorError = data.readErrors.find((item) => item.key === 'pages')
  checks.push(!data.policy || !authorId
    ? { key: 'author', label: 'Verified author', state: 'needs_review', description: 'Configure a publishing author in policy settings, then verify that WordPress returns the same author.' }
    : authorError && !authorIds.has(authorId)
      ? { key: 'author', label: 'Verified author', state: 'needs_review', description: 'Author verification could not be refreshed. Review the WordPress connection before trying again.' }
      : authorIds.has(authorId)
        ? { key: 'author', label: 'Verified author', state: 'ready', description: 'The configured publishing author was returned by the authenticated WordPress connection.' }
        : { key: 'author', label: 'Verified author', state: 'needs_review', description: 'The configured author was not returned by the latest WordPress capability check.' })

  return checks
}

function readinessTone(state: AutopilotCheckState): CalendarReadinessTone {
  if (state === 'ready') return 'green'
  if (state === 'paused' || state === 'needs_connection' || state === 'needs_review') return 'amber'
  if (state === 'error' || state === 'unsupported') return 'red'
  return 'slate'
}

function displayOutcomeValue(value: unknown): string | null {
  if (typeof value === 'string' && value.trim()) return truncate(value.trim(), 240)
  if (isRecord(value)) {
    for (const key of ['message', 'reason', 'action', 'title']) {
      if (typeof value[key] === 'string' && value[key].trim()) return truncate(value[key].trim(), 240)
    }
  }
  return null
}

function displayOutcomeList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map(displayOutcomeValue).filter((item): item is string => Boolean(item)).slice(0, 5)
}

function contentAutopilotOutcome(job: { status: string; result?: Record<string, unknown> }): ContentAutopilotOutcome {
  const result = (job.result ?? {}) as ContentAutopilotResult
  const articleStatus = typeof result.article_status === 'string'
    ? result.article_status
    : isRecord(result.article) && typeof result.article.status === 'string' ? result.article.status : null
  const resultStatus = typeof result.status === 'string' ? result.status.toLowerCase() : ''
  const blockers = displayOutcomeList(result.blockers)
  const nextAction = displayOutcomeValue(result.next_action) ?? displayOutcomeList(result.next_actions)[0] ?? null
  const failed = ['failed'].includes(job.status.toLowerCase()) || resultStatus === 'failed'
  const ambiguous = ['ambiguous', 'needs_reconciliation'].includes(job.status.toLowerCase()) || resultStatus === 'ambiguous'
  const gated = job.status.toLowerCase() === 'blocked' || resultStatus === 'gated'
  const published = articleStatus?.toLowerCase() === 'published' || resultStatus === 'published'
  if (published) return { kind: 'published', title: 'One article was published and verified', message: 'The bounded workflow generated, checked, published, and verified one article under the active policy. This does not mean the whole site is optimized.', articleStatus, blockers, nextAction }
  if (ambiguous) return { kind: 'ambiguous', title: 'The remote outcome needs reconciliation', message: 'ForgeSEO did not confirm a safe final outcome, so it will not retry automatically. Review Activity before trying again.', articleStatus, blockers, nextAction }
  if (failed) return { kind: 'failed', title: 'Content autopilot failed safely', message: 'The workflow did not confirm a successful publication. Review the recorded blocker before trying again.', articleStatus, blockers, nextAction }
  if (gated) return { kind: 'gated', title: 'Content autopilot was gated', message: 'The server stopped before publication because a policy, connection, or enrollment check needs attention.', articleStatus, blockers, nextAction }
  if (job.status.toLowerCase() === 'partial' || ['needs_review', 'review_needed'].includes(resultStatus) || articleStatus?.toLowerCase() === 'review_needed') return { kind: 'needs_review', title: 'Content autopilot needs review', message: 'The workflow stopped at a review gate. No successful publication is claimed until the listed checks are resolved.', articleStatus, blockers, nextAction }
  if (!['complete', 'published'].includes(job.status.toLowerCase())) return { kind: 'running', title: 'Content autopilot is still running', message: 'The server has not returned a terminal result yet. Refresh Activity later; ForgeSEO will not start a duplicate request.', articleStatus, blockers, nextAction }
  return { kind: 'needs_review', title: 'Review the content autopilot result', message: 'The server completed the request without a publication confirmation. Review the recorded article state before taking another action.', articleStatus, blockers, nextAction }
}

function outcomeNoticeKind(kind: ContentAutopilotOutcome['kind']): 'info' | 'success' | 'warning' | 'error' {
  if (kind === 'published') return 'success'
  if (kind === 'failed' || kind === 'ambiguous') return 'error'
  if (kind === 'gated' || kind === 'needs_review') return 'warning'
  return 'info'
}

export function ContentCalendarPage() {
  const siteId = useSiteId()
  const { role } = useAuth()
  const loader = useCallback(async (): Promise<CalendarData> => {
    const [articles, connections, site, policy, settings, pages] = await Promise.all([
      readCalendarSource(() => articlesApi.list(siteId, { limit: 200 })),
      readCalendarSource(() => connectionsApi.list(siteId)),
      readCalendarSource(() => sitesApi.get(siteId)),
      readCalendarSource(() => policyApi.get(siteId)),
      readCalendarSource(() => settingsApi.get()),
      readCalendarSource(() => pagesApi.list(siteId, { limit: 200 })),
    ])
    if (!articles.value) throw new Error(articles.error || 'The article calendar could not be loaded.')
    const normalizedSite = isSite(site.value) ? site.value : null
    const normalizedPolicy = isPolicy(policy.value) ? policy.value : null
    const normalizedSettings = isGlobalSettings(settings.value) ? settings.value : null
    const readErrors = [
      sourceError('connections', connections, 'Connection state was not returned.'),
      normalizedSite ? null : sourceError('site', { ...site, value: null }, 'Site pause state was not returned.'),
      normalizedPolicy ? null : sourceError('policy', { ...policy, value: null }, 'Policy state was not returned.'),
      normalizedSettings ? null : sourceError('settings', { ...settings, value: null }, 'Workspace pause state was not returned.'),
      sourceError('pages', pages, 'Author verification records were not returned.'),
    ].filter((item): item is CalendarReadError => Boolean(item))
    return {
      ...articles.value,
      connections: connections.value?.items ?? [],
      site: normalizedSite,
      policy: normalizedPolicy,
      settings: normalizedSettings,
      pages: pages.value?.items ?? [],
      readErrors,
      refreshedAt: new Date().toISOString(),
    }
  }, [siteId])
  const resource = useResource(loader, [siteId])
  const days = useMemo(() => weekDays(), [])
  const roadmap = useMemo(() => [1, 2, 3, 4].map((week) => ({
    week,
    articles: (resource.data?.items ?? []).filter((article) => !article.scheduled_at && plannedWeek(article) === week),
  })), [resource.data])
  const [planning, setPlanning] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [autopilotWorking, setAutopilotWorking] = useState(false)
  const [autopilotProgress, setAutopilotProgress] = useState<string | null>(null)
  const [autopilotError, setAutopilotError] = useState<string | null>(null)
  const [autopilotOutcome, setAutopilotOutcome] = useState<ContentAutopilotOutcome | null>(null)
  const readiness = useMemo(() => resource.data ? contentAutopilotChecks(resource.data, role, resource.stale, resource.error) : [], [resource.data, resource.error, resource.stale, role])
  const autopilotReady = readiness.length > 0 && readiness.every((check) => check.state === 'ready')

  async function runPlan() {
    if (role === 'viewer') return
    setPlanning(true); setMessage(null); setError(null)
    try {
      const job = await jobsApi.create(siteId, { kind: 'plan', payload: {}, idempotency_key: `plan-${new Date().toISOString().slice(0, 10)}` })
      const finished = await jobsApi.wait(siteId, job.id, { onUpdate: (next) => setMessage(`Content planning is ${next.status}. The server is checking existing intent and enrolled coverage.`) })
      setMessage(finished.status === 'complete' ? 'Content plan refreshed. Weak or overlapping topics were skipped.' : `Content planning is ${finished.status}. Review Activity for the server result.`)
      await resource.reload()
    } catch (requestError) { setError(detailMessage(requestError)) }
    finally { setPlanning(false) }
  }

  async function runContentAutopilot() {
    if (!autopilotReady || autopilotWorking) return
    setAutopilotWorking(true)
    setAutopilotProgress('Content autopilot is being queued. The server will re-check policy and connections before any work starts.')
    setAutopilotError(null)
    setAutopilotOutcome(null)
    try {
      const idempotencyKey = `content-autopilot-${siteId}-${new Date().toISOString().slice(0, 10)}`
      const job = await jobsApi.create(siteId, { kind: 'content_autopilot', payload: { max_articles: 1 }, idempotency_key: idempotencyKey })
      const finished = await jobsApi.wait(siteId, job.id, {
        onUpdate: (next) => {
          const status = next.status.toLowerCase()
          setAutopilotProgress(status === 'queued'
            ? 'Content autopilot is queued. The server is checking policy, connections, and the next qualifying article.'
            : status === 'running'
              ? 'Content autopilot is running one bounded article through research, generation, editorial checks, and publication verification.'
              : `Content autopilot returned a ${status} result. ForgeSEO is refreshing the calendar.`)
        },
      })
      setAutopilotOutcome(contentAutopilotOutcome(finished))
      setAutopilotProgress(null)
      await resource.reload()
    } catch (requestError) {
      setAutopilotProgress(null)
      setAutopilotError(detailMessage(requestError))
    } finally {
      setAutopilotWorking(false)
    }
  }

  return <ResourceStateView resource={resource} empty={<ErrorState message="No article calendar response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => <>
      <div className="page-actions" style={{ marginBottom: 18 }}><Button variant="secondary" onClick={() => void runPlan()} disabled={planning || role === 'viewer'}><Sparkles size={15} /> {planning ? 'Planning...' : 'Plan topics'}</Button></div>
      {message && <div className="mb-20"><Notice kind="success">{message}</Notice></div>}
      {error && <div className="mb-20"><Notice kind="error">{error}</Notice></div>}
      {autopilotProgress && <div className="mb-20"><Notice kind="info" title="Content autopilot">{autopilotProgress}</Notice></div>}
      {autopilotError && <div className="mb-20"><Notice kind="error" title="Content autopilot could not start">{autopilotError}</Notice></div>}
      {autopilotOutcome && <div className="mb-20"><Notice kind={outcomeNoticeKind(autopilotOutcome.kind)} title={autopilotOutcome.title}>
        <p style={{ margin: 0 }}>{autopilotOutcome.message}</p>
        {autopilotOutcome.articleStatus && <p className="text-small" style={{ margin: '8px 0 0' }}>Article state: {titleCase(autopilotOutcome.articleStatus)}</p>}
        {autopilotOutcome.blockers.length > 0 && <><strong style={{ display: 'block', marginTop: 10 }}>Blockers or review reasons</strong><ul className="compact-list" style={{ marginBottom: 0 }}>{autopilotOutcome.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul></>}
        {autopilotOutcome.nextAction && <p className="text-small" style={{ margin: '10px 0 0' }}><strong>Next action:</strong> {autopilotOutcome.nextAction}</p>}
      </Notice></div>}
      <Panel padded>
        <div className="panel-header">
          <div><h2 id="content-autopilot-title" className="panel-title">One-article content autopilot</h2><p className="panel-subtitle">This explicit action may research, generate, check, and publish one policy-approved article. It never means the whole site is optimized.</p></div>
          <Badge value={autopilotReady ? 'ready to run' : 'needs review'} tone={autopilotReady ? 'green' : 'amber'} />
        </div>
        <ul aria-label="Content autopilot readiness checklist" style={{ listStyle: 'none', margin: '16px 0 0', padding: 0, borderTop: '1px solid var(--line)' }}>
          {readiness.map((check) => <li key={check.key} style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: 12, padding: '11px 0', borderBottom: '1px solid var(--line)', alignItems: 'start' }}><div><div className="text-small" style={{ fontWeight: 700 }}>{check.label}</div><div className="text-small text-muted">{check.description}</div></div><Badge value={check.state} tone={readinessTone(check.state)} /></li>)}
        </ul>
        <div className="form-actions" style={{ marginTop: 16 }}><Button aria-describedby="content-autopilot-help" onClick={() => void runContentAutopilot()} disabled={!autopilotReady || autopilotWorking}><Sparkles size={15} /> {autopilotWorking ? 'Running content autopilot...' : 'Run content autopilot'}</Button>{!autopilotReady && <Link className="link-button" to={`/sites/${siteId}/settings/policies`}>Review policy and connections</Link>}</div>
        <p id="content-autopilot-help" className="text-small text-muted" style={{ margin: '10px 0 0' }}>The button stays disabled for viewers, paused sites, missing or stale readiness, disabled publishing policy, unverified connections, or an unverified author. The API may still gate the request.</p>
      </Panel>
      <Panel padded>
        <div className="panel-header">
          <div><h2 className="panel-title">Connection readiness</h2><p className="panel-subtitle">These checks show whether the calendar can reach its sources. A connected source does not guarantee content quality or authorize publication.</p></div>
          <div className="text-small text-muted" aria-label={`Calendar view last refreshed ${formatDateTime(data.refreshedAt)}`}>Calendar view last refreshed: {formatDateTime(data.refreshedAt)}</div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 12 }}>
          <CalendarConnectionCard id="wordpress-calendar-readiness" title="WordPress connection" connection={data.connections.find((connection) => connection.kind === 'wordpress')} readiness={calendarReadiness(data.connections.find((connection) => connection.kind === 'wordpress'))} description="Needed for authenticated inventory and any governed WordPress publishing workflow." />
          <CalendarConnectionCard id="ai-calendar-readiness" title="AI research provider" connection={data.connections.find((connection) => connection.kind === 'ai')} readiness={calendarReadiness(data.connections.find((connection) => connection.kind === 'ai'))} description="Optional for provider-specific research samples. Samples still need source checks and editorial review." />
        </div>
      </Panel>
      <Panel padded><div className="panel-header"><div><h2 className="panel-title">Rolling four-week plan</h2><p className="panel-subtitle">Briefs are grouped by the planner’s week. They are not publication dates until an editor checks and schedules them.</p></div><Badge value="planning view" /></div><div className="content-roadmap">{roadmap.map(({ week, articles }) => <section className="content-roadmap-week" key={week}><div className="content-roadmap-heading"><span>Week {week}</span><Badge value={`${articles.length}`} /></div>{articles.length ? articles.map((article) => <Link to={`/sites/${siteId}/content/articles/${article.id}`} className="calendar-card calendar-card-planned" key={article.id}><div className="calendar-card-status">Planned brief</div><div className="calendar-card-title">{article.title || 'Untitled article'}</div></Link>) : <div className="content-roadmap-empty">No qualifying topic yet.</div>}</section>)}</div></Panel>
      <PageHeader eyebrow="Content" title="Calendar" description="See the work that exists in the API, then open a brief when you’re ready to edit it." actions={<><Button variant="secondary" onClick={() => void resource.reload()} disabled={resource.loading}><RefreshCw size={15} /> Refresh</Button><Link to={`/sites/${siteId}/content/new`} className="button button-primary"><Plus size={16} /> New article</Link></>} />
      <div className="panel-header"><div><h2 className="panel-title">This week</h2><p className="panel-subtitle">Scheduled dates are shown in your browser’s local time. Unscheduled articles stay below.</p></div><Badge value={`${data.total} articles`} /></div>
      <div className="calendar-grid">{days.map((day) => { const dayArticles = data.items.filter((article) => article.scheduled_at && new Date(article.scheduled_at).toDateString() === day.toDateString()); const today = day.toDateString() === new Date().toDateString(); return <div className={`calendar-day ${today ? 'today' : ''}`} key={day.toISOString()}><div className="calendar-day-head"><span className="calendar-day-name">{new Intl.DateTimeFormat('en-US', { weekday: 'short' }).format(day)}</span><span className="calendar-day-number">{day.getDate()}</span></div>{dayArticles.map((article) => <Link to={`/sites/${siteId}/content/articles/${article.id}`} className={`calendar-card ${article.status === 'published' ? 'calendar-card-past' : ''}`} key={article.id}><div className="calendar-card-status">{titleCase(article.status)}</div><div className="calendar-card-title">{article.title || 'Untitled article'}</div></Link>)}</div>})}</div>
      <div className="overview-section"><div className="section-heading"><h2>Unscheduled work</h2><span className="text-small text-muted">{data.items.filter((article) => !article.scheduled_at).length} without a date</span></div><Panel padded={false}>{data.items.filter((article) => !article.scheduled_at).length ? <TableShell caption="Unscheduled articles"><thead><tr><th>Article</th><th>Status</th><th>Updated</th><th /></tr></thead><tbody>{data.items.filter((article) => !article.scheduled_at).map((article) => <tr key={article.id}><td><div className="table-primary">{article.title || 'Untitled article'}</div><div className="table-secondary">{article.slug ? `/${article.slug}` : 'No slug yet'}</div></td><td><Badge value={article.status} /></td><td className="text-muted">{formatDateTime(article.updated_at)}</td><td className="text-right"><Link className="link-button" to={`/sites/${siteId}/content/articles/${article.id}`}>Open editor</Link></td></tr>)}</tbody></TableShell> : <EmptyState icon={<CalendarDays size={20} />} title="Nothing waiting for a date" description="Create an article or open a planned brief to put real work on the calendar." action={<Link to={`/sites/${siteId}/content/new`} className="button button-secondary button-sm"><Plus size={14} /> New article</Link>} />}</Panel></div>
    </>}
  </ResourceStateView>
}

export function ArticlesPage() {
  const siteId = useSiteId()
  const loader = useCallback(() => articlesApi.list(siteId, { limit: 200 }), [siteId])
  const resource = useResource(loader, [siteId])
  return <ResourceStateView resource={resource} empty={<ErrorState message="No article list response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => <>
      <PageHeader eyebrow="Content" title="Articles" description="A direct list of briefs, drafts, scheduled work, and published records." actions={<Link to={`/sites/${siteId}/content/new`} className="button button-primary"><Plus size={16} /> New article</Link>} />
      <Panel padded={false}>{data.items.length ? <TableShell caption="Articles"><thead><tr><th>Article</th><th>Status</th><th>Author</th><th>Scheduled</th><th>Updated</th><th /></tr></thead><tbody>{data.items.map((article) => <tr key={article.id}><td><div className="table-primary">{article.title || 'Untitled article'}</div><div className="table-secondary">{article.slug ? `/${article.slug}` : 'No slug yet'} · {article.managed ? 'Managed' : 'Unmanaged'}</div></td><td><Badge value={article.status} /></td><td className="text-muted">{article.author_id || 'Not assigned'}</td><td className="text-muted">{formatDateTime(article.scheduled_at, 'Unscheduled')}</td><td className="text-muted">{formatDateTime(article.updated_at)}</td><td className="text-right"><Link className="link-button" to={`/sites/${siteId}/content/articles/${article.id}`}>Open</Link></td></tr>)}</tbody></TableShell> : <EmptyState icon={<FileText size={20} />} title="No articles yet" description="Create a real brief when you know what your audience needs. Nothing is generated automatically from this empty state." action={<Link to={`/sites/${siteId}/content/new`} className="button button-primary button-sm"><Plus size={14} /> Create an article</Link>} />}</Panel>
    </>}
  </ResourceStateView>
}

interface EditorData { article: Article | null; revisions: Revision[]; connections: Connection[]; pages: PageRecord[] }

function persistedCheck(article?: Article | null): CheckResult | null {
  const checks = article?.checks
  if (!checks || typeof checks.passed !== 'boolean') return null
  return {
    ...checks,
    blockers: Array.isArray(checks.blockers) ? checks.blockers.map(String) : [],
    warnings: Array.isArray(checks.warnings) ? checks.warnings.map(String) : [],
  }
}

export function ArticleEditorPage() {
  const siteId = useSiteId()
  const { articleId } = useParams()
  const navigate = useNavigate()
  const { role } = useAuth()
  const existingId = articleId
  const canEdit = role === 'owner' || role === 'editor'
  const loader = useCallback(async (): Promise<EditorData> => {
    const [article, revisions, connections, pages] = await Promise.all([
      existingId ? articlesApi.get(siteId, existingId) : Promise.resolve(null),
      existingId ? articlesApi.revisions(siteId, existingId) : Promise.resolve({ items: [] as Revision[] }),
      connectionsApi.list(siteId),
      pagesApi.list(siteId, { limit: 200 }),
    ])
    return { article, revisions: revisions.items, connections: connections.items, pages: pages.items }
  }, [existingId, siteId])
  const resource = useResource(loader, [siteId, existingId])
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [briefAngle, setBriefAngle] = useState('')
  const [briefKeyword, setBriefKeyword] = useState('')
  const [briefOutline, setBriefOutline] = useState('')
  const [sources, setSources] = useState('')
  const [imageSources, setImageSources] = useState<ImageSourceRecord[]>([])
  const [authorId, setAuthorId] = useState('')
  const [scheduledAt, setScheduledAt] = useState('')
  const [initializedFor, setInitializedFor] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [check, setCheck] = useState<CheckResult | null>(null)
  const [selectedRevision, setSelectedRevision] = useState<Revision | null>(null)
  const verifiedAuthors = useMemo(() => {
    const values = new Map<string, string>()
    const wordpress = resource.data?.connections.find((connection) => connection.kind === 'wordpress')
    const authenticated = wordpress?.capabilities?.authenticated_author
    if (authenticated && typeof authenticated === 'object') {
      const author = authenticated as Record<string, unknown>
      if (author.id !== undefined && String(author.id).trim()) values.set(String(author.id), String(author.name || `WordPress author ${author.id}`))
    }
    for (const page of resource.data?.pages.filter((item) => item.resource_type === 'authors') ?? []) {
      const id = page.resource_key.split(':').slice(1).join(':')
      if (id) values.set(id, page.title || `WordPress author ${id}`)
    }
    return Array.from(values, ([id, name]) => ({ id, name }))
  }, [resource.data])
  const authorIsVerified = !authorId || verifiedAuthors.some((author) => author.id === authorId)
  const recordMatchesRoute = Boolean(resource.data && (existingId
    ? resource.data.article?.id === existingId
    : resource.data.article === null))
  const editorReady = recordMatchesRoute && initializedFor === (existingId ?? 'new')

  useEffect(() => {
    // useResource retains the previous route's data while the next read loads.
    // Never mark /articles/:id initialized using /new's null article (or a
    // different article), which would blank the form and suppress real hydration.
    if (!resource.data || !recordMatchesRoute || initializedFor === (existingId ?? 'new')) return
    const article = resource.data.article
    setTitle(article?.title ?? '')
    setBody(article?.body ?? '')
    const brief = article?.brief ?? {}
    setBriefAngle(typeof brief.angle === 'string' ? brief.angle : '')
    setBriefKeyword(typeof brief.target_keyword === 'string' ? brief.target_keyword : '')
    setBriefOutline(typeof brief.outline === 'string' ? brief.outline : '')
    setImageSources(normalizeImageSources(brief.image_sources))
    setSources((article?.sources ?? []).map((source) => typeof source === 'string' ? source : JSON.stringify(source)).join('\n'))
    setAuthorId(article?.author_id ?? '')
    setScheduledAt(toDateTimeLocal(article?.scheduled_at))
    setInitializedFor(existingId ?? 'new')
  }, [existingId, initializedFor, resource.data, recordMatchesRoute])

  useEffect(() => {
    setCheck(persistedCheck(resource.data?.article))
  }, [resource.data])

  const sourcesList = () => sources.split('\n').map((value) => value.trim()).filter(Boolean).map((value) => {
    // Existing structured citations are displayed as JSON, not flattened into
    // opaque strings on the next save. Preserve their evidence identifiers.
    if (value.startsWith('{')) {
      const source = JSON.parse(value)
      if (!source || typeof source !== 'object' || Array.isArray(source)) throw new Error('Each source must be a URL or a source object.')
      return source
    }
    return value
  })
  const brief = { ...resource.data?.article?.brief, angle: briefAngle.trim(), target_keyword: briefKeyword.trim(), outline: briefOutline.trim(), image_sources: imageSourcesPayload(imageSources) }

  async function save(event?: FormEvent) {
    event?.preventDefault()
    if (!editorReady) { setError('Wait for the selected article to load before saving.'); return }
    if (!canEdit) {
      setError('Editor access is required to change or publish articles.')
      return
    }
    setSaving(true)
    setError(null)
    setMessage(null)
    try {
      if (!title.trim()) throw new Error('Add a title before saving the article.')
      if (!authorIsVerified) throw new Error('Choose an author returned by the authenticated WordPress connection, or clear the author field.')
      const imageSourceError = imageSourceValidationError(imageSources)
      if (imageSourceError) throw new Error(imageSourceError)
      if (existingId) {
        await articlesApi.update(siteId, existingId, { title: title.trim(), body, brief, sources: sourcesList(), author_id: authorId.trim() || null, scheduled_at: scheduledAt ? fromDateTimeLocal(scheduledAt) : null })
        setMessage('Article saved. The API now has the latest editor state.')
        await resource.reload()
      } else {
        const article = await articlesApi.create(siteId, { title: title.trim(), brief, sources: sourcesList(), author_id: authorId.trim() || undefined })
        const saved = body || authorId.trim() ? await articlesApi.update(siteId, article.id, { title: title.trim(), body, brief, sources: sourcesList(), author_id: authorId.trim() || null }) : article
        setMessage('Article created. You can now check, schedule, or publish it.')
        navigate(`/sites/${siteId}/content/articles/${saved.id}`, { replace: true })
      }
    } catch (requestError) {
      setError(detailMessage(requestError))
    } finally {
      setSaving(false)
    }
  }

  async function runCheck() {
    if (!existingId) { setError('Save the article first so the API can check its stored body and provenance.'); return }
    if (!canEdit) { setError('Editor access is required to run an editorial check.'); return }
    setSaving(true); setError(null); setMessage(null)
    try { const result = await articlesApi.check(siteId, existingId); setCheck(result); setMessage(result.passed ? 'The editorial check passed.' : 'The editorial check found blockers to resolve.'); await resource.reload() }
    catch (requestError) { setError(detailMessage(requestError)) }
    finally { setSaving(false) }
  }

  async function schedule() {
    if (!existingId) { setError('Save the article before scheduling it.'); return }
    if (!canEdit) { setError('Editor access is required to schedule an article.'); return }
    if (!scheduledAt) { setError('Choose a schedule time first.'); return }
    setSaving(true); setError(null); setMessage(null)
    try { await articlesApi.schedule(siteId, existingId, fromDateTimeLocal(scheduledAt)); setMessage('Schedule request accepted by the API.'); await resource.reload() }
    catch (requestError) { setError(detailMessage(requestError)) }
    finally { setSaving(false) }
  }

  async function publish() {
    if (!existingId) { setError('Save the article before publishing it.'); return }
    if (!canEdit) { setError('Editor access is required to publish an article.'); return }
    setSaving(true); setError(null); setMessage(null)
    try {
      const job = await articlesApi.publish(siteId, existingId)
      const finished = await jobsApi.wait(siteId, job.id, { onUpdate: (next) => setMessage(`Publication is ${next.status}. The server is verifying the remote result.`) })
      setMessage(finished.status === 'complete' ? 'Publication completed and verification is recorded.' : `Publication is ${finished.status}. Review the publication and incident records before retrying.`)
      await resource.reload()
    }
    catch (requestError) { setError(detailMessage(requestError)) }
    finally { setSaving(false) }
  }

  async function generate() {
    if (!existingId) { setError('Save the article before asking the connected AI provider for a draft.'); return }
    if (!canEdit) { setError('Editor access is required to generate a draft.'); return }
    setSaving(true); setError(null); setMessage(null)
    try {
      const job = await jobsApi.create(siteId, { kind: 'generate', payload: { article_id: existingId }, idempotency_key: `generate:${existingId}:${Date.now()}` })
      const finished = await jobsApi.wait(siteId, job.id, { onUpdate: (next) => setMessage(`Draft generation is ${next.status}. Research and editorial checks remain separate.`) })
      setMessage(finished.status === 'complete' ? 'Draft generation finished. Review the stored body and check result before scheduling.' : `Draft generation is ${finished.status}. Review the connection, budget, and research result.`)
      await resource.reload()
    } catch (requestError) { setError(detailMessage(requestError)) }
    finally { setSaving(false) }
  }

  async function rollback() {
    if (!existingId || !window.confirm('Ask the API to roll back the last publication for this article?')) return
    setSaving(true); setError(null); setMessage(null)
    try {
      const job = await articlesApi.rollback(siteId, existingId)
      const finished = await jobsApi.wait(siteId, job.id, { onUpdate: (next) => setMessage(`Rollback is ${next.status}. The server is checking the captured snapshot.`) })
      setMessage(finished.status === 'complete' ? 'Rollback completed and the remote record was verified.' : `Rollback is ${finished.status}. Review the incident record.`)
      await resource.reload()
    }
    catch (requestError) { setError(detailMessage(requestError)) }
    finally { setSaving(false) }
  }

  return <ResourceStateView resource={resource} empty={<ErrorState message="No editor response was returned." onRetry={() => void resource.reload()} />}>
    {() => <>
      <PageHeader eyebrow={existingId ? 'Content / Editor' : 'Content / New article'} title={existingId ? (title || 'Untitled article') : 'New article'} description={existingId ? 'Edit the stored source, check it against business facts, then choose when the API may act.' : 'Start with a grounded brief. Save before asking the API to check or schedule anything.'} actions={<Link className="button button-secondary" to={`/sites/${siteId}/content`}><ArrowLeft size={15} /> Back to calendar</Link>} />
      {message && <div className="mb-20"><Notice kind="success">{message}</Notice></div>}{error && <div className="mb-20"><Notice kind="error">{error}</Notice></div>}
      {!canEdit && <div className="mb-20"><Notice kind="warning" title="Read-only for your role">Viewer access can review article state, but editor access is required to save, check, schedule, publish, or roll back content.</Notice></div>}
      <fieldset disabled={!canEdit || !editorReady} style={{ border: 0, padding: 0, margin: 0 }}>
      <div className="editor-layout">
        <Panel className="editor-card"><form onSubmit={(event) => void save(event)}><div className="stack-sm"><Field label="Title" required><input className="editor-title-input" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="A clear, complete article title" /></Field><div className="form-grid"><Field label="Target keyword"><input value={briefKeyword} onChange={(event) => setBriefKeyword(event.target.value)} placeholder="Optional, grounded keyword" /></Field><Field label="Publishing author" hint={verifiedAuthors.length ? 'Only authors returned by the authenticated WordPress connection can be selected.' : 'Test WordPress to load authors, or leave this blank for a review-only draft.'}><select aria-label="Publishing author" value={authorId} onChange={(event) => setAuthorId(event.target.value)}><option value="">No author selected</option>{authorId && !authorIsVerified && <option value={authorId}>Unverified configured author ({authorId})</option>}{verifiedAuthors.map((author) => <option key={author.id} value={author.id}>{author.name} ({author.id})</option>)}</select></Field><Field label="Editorial angle" hint="What useful question should this answer?"><textarea value={briefAngle} onChange={(event) => setBriefAngle(event.target.value)} placeholder="Describe the reader's need and the useful answer." /></Field><Field label="Outline" hint="Stored as brief metadata for review."><textarea value={briefOutline} onChange={(event) => setBriefOutline(event.target.value)} placeholder="H2s or the shape of the answer" /></Field><Field label="Sources" hint="One URL or source reference per line." ><textarea value={sources} onChange={(event) => setSources(event.target.value)} placeholder="https://example.com/confirmed-source" /></Field></div>{authorId && !authorIsVerified && <Notice kind="warning">This author is not verified by the current WordPress connection. Clear it or test the connection before saving.</Notice>}<Field label="Body" hint="The API checks the stored body for provenance, completeness, and policy before publication."><textarea className="editor-body" value={body} onChange={(event) => setBody(event.target.value)} placeholder="Write or paste the article body here…" /></Field></div><div className="editor-footer"><span className="text-small text-muted">{existingId ? `Last saved ${formatDateTime(resource.data?.article?.updated_at)}` : 'Not saved yet'}</span><div className="editor-actions"><Button variant="secondary" type="submit" disabled={saving}><Save size={15} /> {saving ? 'Saving…' : 'Save article'}</Button>{existingId && <Button variant="ghost" type="button" onClick={() => void runCheck()} disabled={saving}><ShieldAlert size={15} /> Check</Button>}</div></div></form></Panel>
        <div className="stack">
          {existingId && <Panel padded><div className="stack-sm"><strong>Connected draft generation</strong><span className="text-small text-muted">Research, provider cost, and editorial checks are recorded before a draft can be scheduled.</span><Button variant="secondary" onClick={() => void generate()} disabled={saving || role === 'viewer'}><Sparkles size={14} /> Generate draft</Button></div></Panel>}
          {existingId && <ProviderUsagePanel brief={resource.data?.article?.brief} siteId={siteId} />}
          <ImageProvenanceEditor records={imageSources} onChange={setImageSources} />
          {existingId && <PlanningGuidancePanel brief={resource.data?.article?.brief} />}
          {existingId && <ImageProvenancePlanningPanel brief={resource.data?.article?.brief} />}
          <Panel padded><div className="panel-header"><div><h2 className="panel-title">Workflow</h2><p className="panel-subtitle">Every transition is a real API request.</p></div><Badge value={resource.data?.article?.status ?? 'planned'} /></div><div className="stack-sm"><Field label="Schedule time" hint="Stored as an ISO timestamp with your local input converted first."><input type="datetime-local" value={scheduledAt} onChange={(event) => setScheduledAt(event.target.value)} /></Field><div className="editor-actions"><Button size="sm" variant="secondary" onClick={() => void schedule()} disabled={saving || !existingId}><Clock3 size={14} /> Schedule</Button><Button size="sm" onClick={() => void publish()} disabled={saving || !existingId}><Send size={14} /> Publish</Button></div>{existingId && resource.data?.article?.status === 'published' && <Button size="sm" variant="danger" onClick={() => void rollback()} disabled={saving}><RotateCcw size={14} /> Roll back publication</Button>}</div></Panel>
          <CheckPanel check={check} />
          <RevisionsPanel revisions={resource.data?.revisions ?? []} selected={selectedRevision} onSelect={setSelectedRevision} onLoad={(revision) => { setTitle(revision.title); setBody(revision.body); setSelectedRevision(null); setMessage('Revision loaded into the editor. Save when you are ready.') }} />
        </div>
      </div>
      </fieldset>
    </>}
  </ResourceStateView>
}

function ImageProvenanceEditor({ records, onChange }: { records: ImageSourceRecord[]; onChange: (records: ImageSourceRecord[]) => void }) {
  function update(index: number, changes: Partial<ImageSourceRecord>) {
    onChange(records.map((record, recordIndex) => recordIndex === index ? { ...record, ...changes } : record))
  }

  function updateKind(index: number, kind: ImageSourceKind) {
    const current = records[index]
    if (!current) return
    const next: ImageSourceRecord = { ...current, kind }
    if (kind === 'owner_provided') {
      next.owner_confirmed = current.owner_confirmed === true
      delete next.not_real
    } else if (kind === 'generated_illustration') {
      next.not_real = current.not_real === true
      delete next.owner_confirmed
    } else {
      delete next.owner_confirmed
      delete next.not_real
    }
    onChange(records.map((record, recordIndex) => recordIndex === index ? next : record))
  }

  return <Panel padded><div className="panel-header"><div><h2 className="panel-title">Image provenance</h2><p className="panel-subtitle">Record where each article image comes from before editorial review or publication.</p></div><Button type="button" variant="secondary" size="sm" onClick={() => onChange([...records, { url: '', kind: 'owner_provided', owner_confirmed: false }])} disabled={records.length >= MAX_IMAGE_SOURCES}><Plus size={14} /> Add image record</Button></div><Notice kind="warning">Generated illustrations must be clearly labelled and cannot impersonate real products, premises, or completed work.</Notice><p className="text-small text-muted">Use a public http:// or https:// URL. Licensed images require attribution and a license; owner-provided images require confirmation; generated illustrations require a disclosure and a not-real confirmation. Up to {MAX_IMAGE_SOURCES} records are retained.</p>{records.length ? <div className="stack-sm">{records.map((record, index) => <div key={`image-source-${index}`} className="stack-sm" style={{ border: '1px solid #d8e2e1', borderRadius: 8, padding: 14 }}><div className="form-grid"><Field label={`Image ${index + 1} URL`} required hint="Public http:// or https:// image URL."><input aria-label={`Image ${index + 1} URL`} value={record.url} onChange={(event) => update(index, { url: event.target.value })} placeholder="https://cdn.example/image.jpg" /></Field><Field label={`Image ${index + 1} provenance kind`} required><select aria-label={`Image ${index + 1} provenance kind`} value={record.kind} onChange={(event) => updateKind(index, event.target.value as ImageSourceKind)}>{IMAGE_SOURCE_KINDS.map((kind) => <option key={kind} value={kind}>{imageSourceKindLabel(kind)}</option>)}</select></Field><Field label={`Image ${index + 1} attribution`} required={record.kind === 'licensed'} hint={record.kind === 'licensed' ? 'Required for licensed images.' : 'Optional when applicable.'}><input aria-label={`Image ${index + 1} attribution`} value={record.attribution ?? ''} onChange={(event) => update(index, { attribution: event.target.value })} placeholder="Creator or source name" /></Field><Field label={`Image ${index + 1} license`} required={record.kind === 'licensed'} hint={record.kind === 'licensed' ? 'Required for licensed images.' : 'Optional when applicable.'}><input aria-label={`Image ${index + 1} license`} value={record.license ?? ''} onChange={(event) => update(index, { license: event.target.value })} placeholder="CC BY 4.0 or license URL" /></Field><Field label={`Image ${index + 1} alt text`} hint="Describe the image meaningfully; decorative images may use empty alt text when appropriate."><input aria-label={`Image ${index + 1} alt text`} value={record.alt ?? ''} onChange={(event) => update(index, { alt: event.target.value })} placeholder="Meaningful description" /></Field></div>{record.kind === 'owner_provided' && <label className="checkbox-field"><input type="checkbox" aria-label={`Image ${index + 1} owner confirmed`} checked={record.owner_confirmed === true} onChange={(event) => update(index, { owner_confirmed: event.target.checked })} /><span>I confirm this image was provided by the site owner.</span></label>}{record.kind === 'generated_illustration' && <><Field label={`Image ${index + 1} disclosure`} required hint="Label this as generated wherever the image appears."><input aria-label={`Image ${index + 1} disclosure`} value={record.disclosure ?? ''} onChange={(event) => update(index, { disclosure: event.target.value })} placeholder="AI-generated illustration; not a real product or premises" /></Field><label className="checkbox-field"><input type="checkbox" aria-label={`Image ${index + 1} not real confirmation`} checked={record.not_real === true} onChange={(event) => update(index, { not_real: event.target.checked })} /><span>I confirm this illustration is not a real product, premises, or completed work.</span></label></>}<Button type="button" variant="ghost" size="sm" onClick={() => onChange(records.filter((_, recordIndex) => recordIndex !== index))} aria-label={`Remove image ${index + 1}`}>Remove image record</Button></div>)}</div> : <p className="text-small text-muted">No image provenance records added yet.</p>}</Panel>
}

function PlanningGuidancePanel({ brief }: { brief?: Record<string, unknown> }) {
  const links = Array.isArray(brief?.internal_link_opportunities)
    ? brief.internal_link_opportunities.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && typeof (item as Record<string, unknown>).target_url === 'string'))
    : []
  const schema = brief?.structured_data_recommendation && typeof brief.structured_data_recommendation === 'object'
    ? brief.structured_data_recommendation as Record<string, unknown>
    : null
  const research = Array.isArray(brief?.research_evidence)
    ? brief.research_evidence.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)))
    : []
  if (!links.length && !schema && !research.length) return null
  const prerequisites = schema?.prerequisites && typeof schema.prerequisites === 'object' ? schema.prerequisites as Record<string, unknown> : {}
  return <Panel padded><div className="panel-header"><div><h2 className="panel-title">Planning guidance</h2><p className="panel-subtitle">Evidence-backed suggestions for review. ForgeSEO will not add links or structured data automatically from this panel.</p></div><Badge value="review only" /></div><div className="stack-sm">{research.length > 0 && <div><strong>Connected research observations</strong><ul className="compact-list">{research.map((item, index) => { const kind = typeof item.kind === 'string' ? item.kind : 'observation'; const label = typeof item.query === 'string' ? item.query : typeof item.competitor_url === 'string' ? item.competitor_url : 'Provider observation'; const source = typeof item.source === 'string' ? item.source : 'Unknown source'; const observed = typeof item.observed_at === 'string' ? item.observed_at : ''; return <li key={`${kind}-${index}`}><span>{truncate(label, 120)}</span><span className="text-small text-muted"> · {titleCase(kind)} · {source}{observed ? ` · ${formatDateTime(observed)}` : ''}</span></li> })}</ul><span className="text-small text-muted">These observations inform planning only; they are not ranking guarantees or publication authorization.</span></div>}{links.length > 0 && <div><strong>Suggested internal links</strong><ul className="compact-list">{links.map((item, index) => <li key={`${String(item.target_url)}-${index}`}><a href={String(item.target_url)} target="_blank" rel="noreferrer">{String(item.anchor_text || item.target_title || item.target_url)}</a>{Array.isArray(item.matched_terms) && item.matched_terms.length > 0 && <span className="text-small text-muted"> · matched {item.matched_terms.join(', ')}</span>}</li>)}</ul></div>}{schema && <div><strong>{String(schema.schema_type || 'Article')} structured-data prerequisites</strong><div className="check-list">{Object.entries(prerequisites).map(([key, value]) => { const record = value && typeof value === 'object' ? value as Record<string, unknown> : {}; return <div className="check-row warn" key={key}>{key}: {String(record.status || 'needs review')}</div> })}</div><span className="text-small text-muted">The recommendation remains review-required until the author and publisher facts are explicitly verified.</span></div>}</div></Panel>
}

function ImageProvenancePlanningPanel({ brief }: { brief?: Record<string, unknown> }) {
  const imageSources = normalizeImageSources(brief?.image_sources)
  if (!imageSources.length) return null
  return <Panel padded><div className="panel-header"><div><h2 className="panel-title">Planning guidance · image provenance</h2><p className="panel-subtitle">Stored image records are shown with only their allowlisted provenance fields.</p></div><Badge value="review only" /></div><div className="stack-sm"><ul className="compact-list">{imageSources.map((record, index) => { const details = [imageSourceKindLabel(record.kind), record.attribution ? `Attribution: ${truncate(record.attribution, 120)}` : '', record.license ? `License: ${truncate(record.license, 120)}` : '', record.alt ? `Alt: ${truncate(record.alt, 160)}` : '', record.disclosure ? `Disclosure: ${truncate(record.disclosure, 160)}` : '', record.kind === 'owner_provided' ? (record.owner_confirmed ? 'Owner confirmed' : 'Owner confirmation needed') : '', record.kind === 'generated_illustration' ? (record.not_real ? 'Not-real confirmation recorded' : 'Not-real confirmation needed') : ''].filter(Boolean); return <li key={`${record.url}-${index}`}><a href={record.url} target="_blank" rel="noreferrer">{truncate(record.url, 140)}</a><span className="text-small text-muted"> · {details.join(' · ')}</span></li> })}</ul>{imageSources.some((record) => record.kind === 'generated_illustration') && <Notice kind="warning">Generated illustrations must be clearly labelled and cannot impersonate real products, premises, or completed work.</Notice>}<span className="text-small text-muted">These records inform editorial review only; they do not authorize publication or claim that an image depicts a real product, premises, or completed work.</span></div></Panel>
}

function CheckPanel({ check }: { check: CheckResult | null }) {
  return <Panel padded><div className="panel-header"><div><h2 className="panel-title">Editorial check</h2><p className="panel-subtitle">No check result is implied before the API returns one.</p></div>{check && <Badge value={check.passed ? 'passed' : 'needs review'} />}</div>{!check ? <EmptyState icon={<ShieldAlert size={19} />} title="Not checked yet" description="Save the article, then ask the API to check facts, markup, sources, and policy." /> : <div className="check-list">{check.blockers.map((item) => <div className="check-row block" key={`block-${item}`}><XCircle size={16} />{item}</div>)}{check.warnings.map((item) => <div className="check-row warn" key={`warn-${item}`}><AlertCircle size={16} />{item}</div>)}{check.passed && !check.blockers.length && !check.warnings.length && <div className="check-row pass"><CheckCircle2 size={16} />No blockers or warnings were returned.</div>}</div>}</Panel>
}

function RevisionsPanel({ revisions, selected, onSelect, onLoad }: { revisions: Revision[]; selected: Revision | null; onSelect: (revision: Revision | null) => void; onLoad: (revision: Revision) => void }) {
  return <Panel padded><div className="panel-header"><div><h2 className="panel-title">Revisions</h2><p className="panel-subtitle">Append-only snapshots returned by the API.</p></div><History size={18} color="#148b89" /></div>{revisions.length ? <div>{revisions.map((revision) => <div className="revision-row" key={revision.id}><div><div className="revision-title">{revision.title || 'Untitled'}</div><div className="revision-meta">{formatDateTime(revision.created_at)}{revision.reason ? ` · ${revision.reason}` : ''}</div></div><Button variant="ghost" size="sm" onClick={() => onSelect(selected?.id === revision.id ? null : revision)}>View</Button></div>)}{selected && <div className="stack-sm" style={{ marginTop: 12 }}><div className="json-preview">{truncate(selected.body, 450)}</div><Button variant="secondary" size="sm" onClick={() => onLoad(selected)}>Load into editor</Button></div>}</div> : <EmptyState icon={<History size={19} />} title="No revisions yet" description="Saved versions will appear here after the API records them." />}</Panel>
}
