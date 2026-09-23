import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowDownToLine, BarChart3, CheckCircle2, Clock3, ExternalLink, LifeBuoy, RefreshCw, ScrollText } from 'lucide-react'
import { Badge, Button, EmptyState, ErrorState, LoadingState, Notice, PageHeader, Panel, StaleState, TableShell } from '../components/ui'
import { useAuth } from '../context/AppContext'
import { detailMessage, jobsApi, operationsApi, publicationsApi } from '../lib/api'
import { formatDateTime, titleCase } from '../lib/format'
import type { EventRecord, Incident, Job, JobProgress, Publication, PublicationReconciliationJob } from '../types'
import { useSiteEventStream } from '../lib/useSiteEventStream'
import { ResourceStateView, useResource, useSiteId } from './shared'

const SAFE_ACTIVITY_STATUSES = new Set(['queued', 'running', 'complete', 'partial', 'failed', 'blocked', 'cancelled', 'retrying', 'needs_connection', 'needs_review'])
const SAFE_ACTIVITY_STAGES = new Set(['availability', 'inventory', 'public_audit', 'content_plan', 'refresh_evaluation', 'audit', 'plan', 'generate', 'publish', 'visibility', 'refresh'])

function activityDetails(event: EventRecord) {
  if (!event.data) return '—'
  const status = typeof event.data.status === 'string' && SAFE_ACTIVITY_STATUSES.has(event.data.status.toLowerCase())
    ? titleCase(event.data.status)
    : null
  const stage = typeof event.data.stage === 'string' && SAFE_ACTIVITY_STAGES.has(event.data.stage.toLowerCase())
    ? titleCase(event.data.stage)
    : null
  return [stage, status].filter(Boolean).join(' · ') || 'Recorded by server'
}

export function ActivityPage() {
  const siteId = useSiteId()
  const loader = useCallback(() => operationsApi.activity(siteId, { limit: 200 }), [siteId])
  const resource = useResource(loader, [siteId])
  const [liveEvents, setLiveEvents] = useState<EventRecord[] | null>(null)
  useEffect(() => {
    if (resource.data) setLiveEvents(resource.data.items)
  }, [resource.data])
  const onEvent = useCallback((event: EventRecord) => {
    setLiveEvents((current) => {
      const existing = current ?? []
      const next = [event, ...existing.filter((item) => String(item.id) !== String(event.id))]
      next.sort((left, right) => {
        const leftTime = left.created_at ? Date.parse(left.created_at) : 0
        const rightTime = right.created_at ? Date.parse(right.created_at) : 0
        return rightTime - leftTime || Number(right.id) - Number(left.id)
      })
      return next.slice(0, 200)
    })
  }, [])
  const stream = useSiteEventStream(siteId, { enabled: resource.data !== null, onEvent })
  return <ResourceStateView resource={resource} empty={<ErrorState message="No activity response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => { const items = liveEvents ?? data.items; return <><PageHeader eyebrow="Operations" title="Activity" description="A chronological record from the server—audits, decisions, jobs, and source observations." actions={<><Badge value={stream.status === 'connected' ? 'live' : stream.status} /><button className="button button-secondary" onClick={() => void resource.reload()}><RefreshCw size={15} /> Refresh</button></>} /><Panel padded={false}>{items.length ? <TableShell caption="Activity stream"><thead><tr><th>Event</th><th>Kind</th><th>When</th><th>Details</th></tr></thead><tbody>{items.map((event) => <tr key={String(event.id)}><td><div className="table-primary">{event.message}</div></td><td><Badge value={event.kind} /></td><td className="text-muted">{formatDateTime(event.created_at)}</td><td className="text-muted">{activityDetails(event)}</td></tr>)}</tbody></TableShell> : <EmptyState icon={<ScrollText size={20} />} title="No activity recorded" description="No server events are recorded for this site yet. Run an audit to create the first evidence trail; an empty activity stream is not proof that the site is optimized." action={<Link className="button button-secondary button-sm" to={`/sites/${siteId}/overview`}>Open overview to run an audit</Link>} />}</Panel></> }}
  </ResourceStateView>
}

export function JobsPage() {
  const siteId = useSiteId()
  const loader = useCallback(() => jobsApi.list(siteId, { limit: 200 }), [siteId])
  const resource = useResource(loader, [siteId])
  const [liveProgress, setLiveProgress] = useState<JobProgress | null>(null)
  const onProgress = useCallback((progress: JobProgress) => setLiveProgress(progress), [])
  const stream = useSiteEventStream(siteId, { enabled: resource.data !== null, onProgress })
  useEffect(() => { setLiveProgress(null) }, [siteId])

  return <>
    <PageHeader eyebrow="Operations" title="Run history" description="Recent jobs recorded for this site, including their current status and retry count." actions={<button className="button button-secondary" onClick={() => void resource.reload()}><RefreshCw size={15} /> Refresh</button>} />
    {resource.loading && !resource.data ? <LoadingState label="Loading recent jobs" /> : resource.error && !resource.data ? <ErrorState message={resource.error} onRetry={() => void resource.reload()} /> : !resource.data ? <ErrorState message="No jobs response was returned." onRetry={() => void resource.reload()} /> : <>
      <JobProgressBanner progress={liveProgress} streamStatus={stream.status} />
      {resource.stale && <StaleState onRefresh={() => void resource.reload()} />}
      {resource.error && <div className="mt-20"><ErrorState message={resource.error} onRetry={() => void resource.reload()} /></div>}
      <Panel padded={false}>
        {resource.data.items.length ? <JobTable jobs={resource.data.items} /> : <EmptyState icon={<Clock3 size={20} />} title="No jobs yet" description="No runs have been recorded for this site. An empty run history is not proof that the site is optimized; check audit coverage and findings." />}
      </Panel>
    </>}
  </>
}

function jobKindLabel(kind: JobProgress['kind']) {
  switch (kind) {
    case 'full_cycle': return 'Full cycle'
    case 'audit': return 'Audit'
    case 'inventory': return 'Inventory'
    case 'plan': return 'Content plan'
    case 'generate': return 'Content generation'
    case 'publish': return 'Publication'
    case 'availability': return 'Availability check'
    case 'visibility': return 'Visibility check'
    case 'refresh': return 'Refresh'
    default: return 'Workflow'
  }
}

function stageLabel(stage: JobProgress['stage']) {
  switch (stage) {
    case 'availability': return 'Availability check'
    case 'inventory': return 'Inventory'
    case 'public_audit': return 'Public audit'
    case 'content_plan': return 'Content planning'
    case 'refresh_evaluation': return 'Refresh evaluation'
    case 'audit': return 'Audit'
    case 'plan': return 'Content planning'
    case 'generate': return 'Content generation'
    case 'publish': return 'Publication'
    case 'visibility': return 'Visibility check'
    case 'refresh': return 'Refresh'
    default: return null
  }
}

function progressStatusMessage(progress: JobProgress) {
  const kind = jobKindLabel(progress.kind)
  const stage = stageLabel(progress.stage)
  switch (progress.status) {
    case 'queued': return `${kind} is queued.`
    case 'running': return `${kind} is running.${stage ? ` ${stage} is in progress.` : ''}`
    case 'complete': return `${kind} completed.`
    case 'partial': return `${kind} finished with some steps needing review.`
    case 'failed': return `${kind} could not be completed.`
    case 'blocked': return `${kind} is waiting for review before it can continue.`
    case 'cancelled': return `${kind} was cancelled.`
    case 'retrying': return `${kind} is retrying.`
    case 'needs_connection': return `${kind} needs a connection before it can continue.`
    case 'needs_review': return `${kind} needs review before it can continue.`
    default: return `An update is available for your ${kind.toLowerCase()}.`
  }
}

function progressNoticeKind(status: JobProgress['status']): 'info' | 'success' | 'warning' | 'error' {
  if (status === 'complete') return 'success'
  if (status === 'failed') return 'error'
  if (status === 'partial' || status === 'blocked' || status === 'needs_connection' || status === 'needs_review') return 'warning'
  return 'info'
}

function JobProgressBanner({ progress, streamStatus }: { progress: JobProgress | null; streamStatus: ReturnType<typeof useSiteEventStream>['status'] }) {
  if (!progress) {
    if (streamStatus === 'unsupported') {
      return <div className="mb-20"><Notice kind="info" title="Live progress is unavailable">Run history is still available. Refresh this page to check for updates.</Notice></div>
    }
    if (streamStatus === 'reconnecting') {
      return <div className="mb-20"><Notice kind="warning" title="Live updates are reconnecting">Run history is still available. New progress updates will appear when the connection is restored.</Notice></div>
    }
    return null
  }

  const reconnecting = streamStatus === 'reconnecting'
  const detail = progress.percent !== undefined
    ? `${progress.percent}% complete.`
    : progress.completed !== undefined && progress.total !== undefined && progress.total > 0
      ? `${Math.min(progress.completed, progress.total)} of ${progress.total} steps complete.`
      : progress.stage_index !== undefined && progress.stage_count !== undefined && progress.stage_count > 0
        ? `Working on step ${Math.min(progress.stage_index, progress.stage_count)} of ${progress.stage_count}.`
        : null
  const message = progressStatusMessage(progress)

  return <div className="mb-20"><Notice kind={progressNoticeKind(progress.status)} title={reconnecting ? 'Live updates are reconnecting' : 'Live run update'}>{message}{detail && ` ${detail}`}{reconnecting && ' New progress updates will appear when the connection is restored.'}</Notice></div>
}

function JobTable({ jobs }: { jobs: Job[] }) {
  return <TableShell caption="Run history">
    <thead><tr><th>Kind</th><th>Status</th><th>Created</th><th>Updated</th><th>Attempts</th><th>Details</th></tr></thead>
    <tbody>{jobs.map((job) => <tr key={job.id}>
      <td><div className="table-primary">{titleCase(job.kind)}</div></td>
      <td><Badge value={job.status} /></td>
      <td className="text-muted">{formatDateTime(job.created_at)}</td>
      <td className="text-muted">{formatDateTime(job.updated_at)}</td>
      <td className="text-muted">{job.attempts ?? 'Not recorded'}</td>
      <td className="text-muted">{inventoryFailureMessage(job)}</td>
    </tr>)}</tbody>
  </TableShell>
}

function inventoryFailureMessage(job: Job) {
  if (!['failed', 'retry', 'partial'].includes(job.status) || job.result?.error_type !== 'IncompleteInventory') return '—'
  switch (job.result.inventory_issue) {
    case 'limit': return 'Page inventory is incomplete. The collection exceeds the current limit for one run. Previously stored pages remain available.'
    case 'pagination': return 'Page inventory is incomplete. The site returned inconsistent pagination; check its WordPress connection before trying again.'
    case 'records': return 'Page inventory is incomplete. The site returned invalid or repeated records; check its WordPress connection before trying again.'
    default: return 'Page inventory is incomplete. Review the site connection before trying again.'
  }
}

type IncidentEvidenceField = { label: string; value: string }

const SAFE_INCIDENT_DETAIL_LABELS: Record<string, string> = {
  status_code: 'Status code',
  queue_delay_seconds: 'Queue delay (seconds)',
  missed_checks: 'Missed checks',
  threshold_seconds: 'Threshold (seconds)',
  due_jobs: 'Due jobs',
  seconds_since_heartbeat: 'Seconds since heartbeat',
  seconds_since_verification: 'Seconds since verification',
  missed_window: 'Missed freshness window',
  last_success_at: 'Last successful check',
  checked_at: 'Checked at',
  channel: 'Channel',
  error_type: 'Error type',
  publication_id: 'Publication',
  article_id: 'Article',
}

const SAFE_INCIDENT_RESOURCE_KEYS = ['affected_resource', 'resource', 'resource_key', 'resource_type', 'resource_id', 'page_url', 'url']
const SAFE_INCIDENT_REASON_KEYS = ['reason', 'error_type']
const INCIDENT_TEXT_LIMIT = 240

function safeIncidentScalar(value: unknown) {
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    const text = String(value).trim().slice(0, INCIDENT_TEXT_LIMIT)
    return text || null
  }
  return null
}

function safeIncidentResource(value: unknown): string | null {
  const scalar = safeIncidentScalar(value)
  if (scalar) return scalar
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null

  const resource = value as Record<string, unknown>
  const parts = [
    safeIncidentScalar(resource.resource_type ?? resource.type),
    safeIncidentScalar(resource.resource_key ?? resource.key),
    safeIncidentScalar(resource.resource_id ?? resource.id),
    safeIncidentScalar(resource.public_url ?? resource.url),
  ].filter((part): part is string => Boolean(part))
  return parts.length ? parts.join(' · ') : null
}

function incidentEvidenceFields(incident: Incident): IncidentEvidenceField[] {
  const details = incident.details && typeof incident.details === 'object' && !Array.isArray(incident.details)
    ? incident.details
    : {}
  const fields: IncidentEvidenceField[] = []
  const usedKeys = new Set<string>()
  const add = (label: string, value: unknown, formatter = safeIncidentScalar) => {
    const formatted = formatter(value)
    if (formatted) fields.push({ label, value: formatted })
  }

  for (const key of SAFE_INCIDENT_RESOURCE_KEYS) {
    if (!(key in details)) continue
    const formatted = safeIncidentResource(details[key])
    if (formatted) {
      add('Affected resource', formatted)
      usedKeys.add(key)
      break
    }
  }

  for (const key of SAFE_INCIDENT_REASON_KEYS) {
    if (!(key in details)) continue
    const formatted = safeIncidentScalar(details[key])
    if (formatted) {
      add('Reason', formatted)
      usedKeys.add(key)
      break
    }
  }

  for (const [key, label] of Object.entries(SAFE_INCIDENT_DETAIL_LABELS)) {
    if (usedKeys.has(key) || !(key in details)) continue
    const value = details[key]
    const formatted = safeIncidentScalar(value)
    if (formatted) {
      add(label, key.endsWith('_at') ? formatDateTime(formatted) : formatted)
      usedKeys.add(key)
    }
  }

  if (incident.first_seen_at) add('First seen', formatDateTime(incident.first_seen_at))
  if (incident.last_seen_at) add('Last seen', formatDateTime(incident.last_seen_at))
  if (incident.resolved_at) add('Resolved', formatDateTime(incident.resolved_at))
  return fields
}

function IncidentEvidence({ incident }: { incident: Incident }) {
  const fields = incidentEvidenceFields(incident)
  return <details className="incident-evidence">
    <summary>View safe evidence</summary>
    {fields.length ? <dl style={{ display: 'grid', gridTemplateColumns: 'minmax(8rem, auto) 1fr', gap: '6px 12px', margin: '10px 0 0' }}>
      {fields.map((field) => <div key={`${field.label}-${field.value}`} style={{ display: 'contents' }}><dt className="text-small text-muted">{field.label}</dt><dd className="text-small" style={{ margin: 0, overflowWrap: 'anywhere' }}>{field.value}</dd></div>)}
    </dl> : <p className="text-small text-muted" style={{ margin: '10px 0 0' }}>No additional safe evidence was recorded.</p>}
  </details>
}

export function IncidentsPage() {
  const siteId = useSiteId()
  const loader = useCallback(() => operationsApi.incidents(siteId, { limit: 200 }), [siteId])
  const resource = useResource(loader, [siteId])
  return <ResourceStateView resource={resource} empty={<ErrorState message="No incidents response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => <><PageHeader eyebrow="Operations" title="Incidents" description="Failures and connector health issues stay visible until the API resolves them." actions={<button className="button button-secondary" onClick={() => void resource.reload()}><RefreshCw size={15} /> Refresh</button>} /><Panel padded={false}>{data.items.length ? <TableShell caption="Incidents"><thead><tr><th>Incident</th><th>Severity</th><th>Status</th><th>Failures</th><th>Last seen</th></tr></thead><tbody>{data.items.map((incident) => <tr key={incident.id}><td><div className="table-primary">{incident.title}</div><div className="table-secondary">{titleCase(incident.kind)} · {incident.key}</div><IncidentEvidence incident={incident} /></td><td><Badge value={incident.severity} /></td><td><Badge value={incident.status} /></td><td className="text-muted">{incident.failure_count ?? 0}</td><td className="text-muted">{formatDateTime(incident.last_seen_at)}</td></tr>)}</tbody></TableShell> : <EmptyState icon={<CheckCircle2 size={20} />} title="No open incidents" description="The API has not recorded an incident for this site. This is a live status, not a synthetic health score." />}</Panel></>}
  </ResourceStateView>
}

const PUBLICATION_RECONCILIATION_STATUSES = new Set(['ambiguous', 'needs_reconciliation'])

function publicationNeedsReconciliation(publication: Publication) {
  const publicationStatus = publication.status.toLowerCase()
  const resultStatus = typeof publication.result?.status === 'string' ? publication.result.status.toLowerCase() : ''
  return PUBLICATION_RECONCILIATION_STATUSES.has(publicationStatus)
    || PUBLICATION_RECONCILIATION_STATUSES.has(resultStatus)
    || (publicationStatus === 'failed' && resultStatus === 'ambiguous')
}

const RECONCILIATION_REASON_MESSAGES: Record<string, string> = {
  reconciliation_unavailable: 'The remote service was unavailable for a safe check.',
  snapshot_mismatch: 'The remote record did not match the saved publication snapshot.',
  remote_record_not_found: 'The expected remote record was not found.',
  no_unique_remote_match: 'The remote service did not return one safe match.',
  remote_state_not_publishable: 'The remote record was found, but its state still needs review.',
}

type ReconciliationFeedback = {
  kind: 'info' | 'success' | 'warning'
  message: string
}

function reconciliationFeedback(job: PublicationReconciliationJob): ReconciliationFeedback {
  const result = job.result
  switch (result?.status) {
    case 'published':
      return { kind: 'success', message: 'The remote publication was confirmed. No new publication was attempted.' }
    case 'draft_reconciled':
      return { kind: 'success', message: 'The remote draft was found and reconciled. No new publication was attempted; it remains ready for an explicit next step.' }
    case 'already_resolved':
      return { kind: 'success', message: 'This publication was already resolved. No new publication was attempted.' }
    case 'held': {
      const reason = typeof result.reason === 'string' ? RECONCILIATION_REASON_MESSAGES[result.reason] : undefined
      return {
        kind: 'warning',
        message: `The remote outcome could not be confirmed safely. No new publication was attempted; this publication remains held for review.${reason ? ` ${reason}` : ''}`,
      }
    }
    default:
      return {
        kind: 'warning',
        message: job.status === 'failed'
          ? 'The remote outcome check did not complete. No new publication was attempted; this publication remains held for review.'
          : 'The remote outcome check finished without a safe resolution. No new publication was attempted; this publication remains held for review.',
      }
  }
}

export function PublicationsPage() {
  const siteId = useSiteId()
  const { role } = useAuth()
  const [working, setWorking] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<ReconciliationFeedback | null>(null)
  const [error, setError] = useState<string | null>(null)
  const loader = useCallback(() => operationsApi.publications(siteId, { limit: 200 }), [siteId])
  const resource = useResource(loader, [siteId])

  async function reconcile(publication: Publication) {
    if (role !== 'owner' || !publicationNeedsReconciliation(publication)) return
    setWorking(publication.id)
    setFeedback({ kind: 'info', message: 'Checking the remote outcome. No new publication will be attempted.' })
    setError(null)
    try {
      const queued = await publicationsApi.reconcile(siteId, publication.id)
      const finished = await jobsApi.wait<PublicationReconciliationJob>(siteId, queued.id, {
        onUpdate: (next) => setFeedback({ kind: 'info', message: `The remote outcome check is ${next.status}. No new publication will be attempted.` }),
      })
      const result = reconciliationFeedback(finished)
      await resource.reload()
      setFeedback(result)
    } catch (requestError) {
      setFeedback(null)
      setError(detailMessage(requestError))
    } finally {
      setWorking(null)
    }
  }

  return <ResourceStateView resource={resource} empty={<ErrorState message="No publications response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => <><PageHeader eyebrow="Operations" title="Publications" description="A server-backed ledger of attempted editorial operations and their outcomes." actions={<button className="button button-secondary" onClick={() => void resource.reload()}><RefreshCw size={15} /> Refresh</button>} />{feedback && <div className="mb-20"><Notice kind={feedback.kind}>{feedback.message}</Notice></div>}{error && <div className="mb-20"><Notice kind="error">{error}</Notice></div>}<Panel padded={false}>{data.items.length ? <TableShell caption="Publications ledger"><thead><tr><th>Operation</th><th>Status</th><th>Policy</th><th>Remote record</th><th>Updated</th><th>Action</th></tr></thead><tbody>{data.items.map((publication) => { const needsReconciliation = publicationNeedsReconciliation(publication); return <tr key={publication.id}><td><div className="table-primary">{publication.article_id ? 'Article publication' : publication.candidate_id ? 'Candidate change' : 'Editorial operation'}</div><div className="table-secondary">Recorded {formatDateTime(publication.created_at)}</div></td><td><Badge value={publication.status} /></td><td className="text-muted">v{publication.policy_version}</td><td className="text-muted">{publication.remote_id ? 'Remote record linked' : 'Not linked'}</td><td className="text-muted">{formatDateTime(publication.updated_at)}</td><td>{needsReconciliation ? role === 'owner' ? <Button variant="secondary" size="sm" type="button" onClick={() => void reconcile(publication)} disabled={working !== null} aria-label="Check remote outcome">{working === publication.id ? 'Checking…' : 'Check remote outcome'}</Button> : <span className="text-small text-muted">Owner access required to check the remote outcome. This view is read-only.</span> : <span className="text-muted">—</span>}</td></tr> })}</tbody></TableShell> : <EmptyState icon={<LifeBuoy size={20} />} title="No publication attempts" description="Approved work will show here once a real publish or candidate execution request reaches the API." />}</Panel></>}
  </ResourceStateView>
}

const REPORT_METADATA_FIELDS = new Set(['generated_at', 'period_start', 'period_end', 'note', 'status', 'partial', 'complete'])
const REQUIRED_REPORT_SECTIONS = ['site', 'overview', 'measurements', 'events', 'publications']
// These sections are not returned by every API version yet. When present, they
// are validated and surfaced as evidence, but their absence is not treated as
// an incomplete report from an older endpoint.
const OPTIONAL_REPORT_SECTIONS = ['incidents', 'spending', 'budget']

type ReportScalar = string | number | boolean

function isReportRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function isReportScalar(value: unknown): value is ReportScalar {
  return ['string', 'number', 'boolean'].includes(typeof value)
}

function reportMetricLabel(key: string) {
  const parts = key.split('.')
  if (parts[0] === 'overview' && parts[1] === 'counts') return titleCase(parts.slice(2).join('_'))
  if (parts.at(-1) === 'total') return `${titleCase(parts.slice(0, -1).join('_'))} total`
  return titleCase(key.replaceAll('.', ' '))
}

function reportEntries(report: Record<string, unknown>) {
  const entries: Array<[string, ReportScalar]> = []
  const add = (key: string, value: unknown) => {
    if (!REPORT_METADATA_FIELDS.has(key) && isReportScalar(value)) entries.push([key, value])
  }

  // Keep top-level scalar fields for forward compatibility, but do not treat
  // the report envelope's timestamps or explanatory note as measures.
  Object.entries(report).forEach(([key, value]) => add(key, value))

  // The current API nests the actual site measures in overview.counts and
  // budget. Read only those known objects; never manufacture counts from
  // array lengths or missing values.
  const overview = isReportRecord(report.overview) ? report.overview : null
  const counts = overview && isReportRecord(overview.counts) ? overview.counts : null
  Object.entries(counts ?? {}).forEach(([key, value]) => add(`overview.counts.${key}`, value))
  const overviewBudget = overview && isReportRecord(overview.budget) ? overview.budget : null
  Object.entries(overviewBudget ?? {}).forEach(([key, value]) => add(`overview.budget.${key}`, value))

  // Collection totals are scalar measures supplied by the API. The items are
  // retained verbatim in the raw response below, including when the total is
  // zero or the collection is empty.
  for (const section of [...REQUIRED_REPORT_SECTIONS, ...OPTIONAL_REPORT_SECTIONS]) {
    const collection = report[section]
    if (!isReportRecord(collection)) continue
    if (section === 'spending') {
      const account = isReportRecord(collection.budget_account) ? collection.budget_account : null
      Object.entries(account ?? {})
        .filter(([key]) => ['limit_cents', 'spent_cents', 'reserved_cents'].includes(key))
        .forEach(([key, value]) => add(`${section}.budget_account.${key}`, value))
      const reservations = isReportRecord(collection.reservations) ? collection.reservations : null
      add(`${section}.reservations.total`, reservations?.total)
      continue
    }
    if (section === 'budget') {
      Object.entries(collection)
        .filter(([key]) => key !== 'items' && key !== 'status')
        .forEach(([key, value]) => add(`${section}.${key}`, value))
      continue
    }
    add(`${section}.total`, collection.total)
    if (!('total' in collection)) add(`${section}.count`, collection.count)
  }

  return entries
}

function reportState(report: Record<string, unknown>, entries: Array<[string, unknown]>) {
  const missingSections = REQUIRED_REPORT_SECTIONS.filter((section) => !isReportRecord(report[section]) && !Array.isArray(report[section]))
  const malformedOptionalSections = OPTIONAL_REPORT_SECTIONS.filter((section) => section in report && !isReportRecord(report[section]) && !Array.isArray(report[section]))
  const status = typeof report.status === 'string' ? report.status.toLowerCase() : ''
  const explicitlyPartial = report.partial === true || report.complete === false || ['empty', 'partial', 'incomplete'].includes(status)
  const empty = Object.keys(report).length === 0
  const noScalarMeasures = entries.length === 0
  const partial = !empty && (missingSections.length > 0 || malformedOptionalSections.length > 0 || explicitlyPartial)
  return { empty, noScalarMeasures, partial }
}

export function WeeklyReportPage() {
  const siteId = useSiteId()
  const loader = useCallback(() => operationsApi.weekly(siteId), [siteId])
  const resource = useResource(loader, [siteId])
  const [downloadError, setDownloadError] = useState<string | null>(null)

  async function download() {
    setDownloadError(null)
    try {
      const blob = await operationsApi.weeklyCsv(siteId)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'forgeseo-weekly-report.csv'
      anchor.click()
      URL.revokeObjectURL(url)
    } catch (error) { setDownloadError(detailMessage(error)) }
  }

  return <ResourceStateView resource={resource} empty={<ErrorState message="No weekly report response was returned." onRetry={() => void resource.reload()} />}>
    {(report) => { const entries = reportEntries(report); const state = reportState(report, entries); return <><PageHeader eyebrow="Operations" title="Weekly report" description="Download the exact report returned by the API, or inspect its scalar measures here. Missing or partial data is not an optimization claim." actions={<><button className="button button-secondary" onClick={() => void resource.reload()}><RefreshCw size={15} /> Refresh</button><button className="button button-primary" onClick={() => void download()}><ArrowDownToLine size={15} /> Download CSV</button></>} />{downloadError && <div className="mb-20"><Notice kind="error">{downloadError}</Notice></div>}{(state.empty || state.partial) && <div className="mb-20"><Notice kind="warning" title={state.empty ? 'Empty weekly report' : 'Partial weekly report'}>{state.noScalarMeasures ? 'The API returned no scalar measures.' : 'The API returned only part of the expected weekly report.'} This is not evidence that the site is optimized; review coverage and source evidence before drawing conclusions.</Notice></div>}<div className="report-hero">{entries.slice(0, 4).map(([key, value]) => <div className="stat-card" key={key}><div className="stat-label">{reportMetricLabel(key)}</div><div className="report-number">{String(value)}</div><div className="report-label">API-reported measure</div></div>)}</div><div className="grid-2 mt-20"><Panel padded><div className="panel-header"><div><h2 className="panel-title">Report measures</h2><p className="panel-subtitle">Only fields present in the weekly response are shown.</p></div><BarChart3 size={19} color="#148b89" /></div>{entries.length ? entries.map(([key, value]) => <div className="metric-row" key={key}><span>{reportMetricLabel(key)}</span><strong>{String(value)}</strong></div>) : <EmptyState icon={<BarChart3 size={20} />} title="No scalar measures" description="The API returned no scalar measures to summarize. This is not evidence that the site is optimized." />}</Panel><Panel padded><div className="panel-header"><div><h2 className="panel-title">Raw report response</h2><p className="panel-subtitle">Useful when the report includes nested source details.</p></div><Clock3 size={18} color="#148b89" /></div><pre className="json-preview">{JSON.stringify(report, null, 2)}</pre></Panel></div></>}}
  </ResourceStateView>
}
