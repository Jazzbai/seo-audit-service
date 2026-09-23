import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { Activity, AlertTriangle, ArrowUpRight, BookOpen, FileText, Gauge, Play, RefreshCw, ShieldCheck, Sparkles } from 'lucide-react'
import { Button, Badge, EmptyState, ErrorState, Notice, PageHeader, Panel, ProgressBar, StatCard } from '../components/ui'
import { useAuth } from '../context/AppContext'
import { sitesApi, jobsApi, policyApi, settingsApi, detailMessage } from '../lib/api'
import { formatCurrencyCents, formatDateTime, formatNumber, percent, titleCase } from '../lib/format'
import type { FullCycleExecutionGate, FullCycleExecutionSummary, FullCycleNextAction, FullCycleResult, FullCycleStage, Overview } from '../types'
import { ResourceStateView, useResource, useSiteId } from './shared'

const FULL_CYCLE_SCOPE = 'It checks availability, inventory/audit, content planning, and refresh evaluation; writes and paid services remain policy/connection-gated.'
const SITE_AUTOPILOT_SCOPE = 'It runs availability, inventory, public audit, content planning, policy-authorized metadata checks, one-article content autopilot, and refresh evaluation. It may write or publish only when the active policy and every verified prerequisite allow it.'

const FULL_CYCLE_STAGE_LABELS: Record<string, string> = {
  availability: 'Availability check',
  inventory: 'WordPress inventory',
  public_audit: 'Public site audit',
  content_plan: 'Content planning',
  refresh_evaluation: 'Article refresh evaluation',
}

const FULL_CYCLE_ACTION_LABELS: Record<string, string> = {
  connect_wordpress: 'Connect WordPress',
  review_incomplete_stages: 'Review incomplete stages',
  review_results: 'Review results',
  publish: 'Publishing',
  metadata_writes: 'Metadata changes',
  paid_visibility: 'Paid visibility research',
  remote_mutations: 'Other remote changes',
}

const SITE_AUTOPILOT_STAGE_LABELS: Record<string, string> = {
  availability: 'Availability check',
  inventory: 'WordPress inventory',
  public_audit: 'Public site audit',
  content_plan: 'Content planning',
  one_article_content_autopilot: 'One-article content autopilot',
  content_autopilot: 'One-article content autopilot',
  refresh_evaluation: 'Article refresh evaluation',
}

type SiteAutopilotResult = {
  status: 'complete' | 'published' | 'gated' | 'needs_review' | 'failed' | 'unknown'
  complete: boolean
  stages: Array<{ name: string; status: string }>
  metadata?: {
    status?: string
    candidatesAuthorized?: number
    jobsQueued?: number
  }
}

type SiteAutopilotReadiness = {
  canRun: boolean
  kind: 'info' | 'warning'
  title: string
  message: string
  canRetry: boolean
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function finiteCount(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.floor(value) : undefined
}

function countFrom(value: unknown, keys: string[]): number | undefined {
  const direct = finiteCount(value)
  if (direct !== undefined) return direct
  if (!isRecord(value)) return undefined
  for (const key of keys) {
    const count = finiteCount(value[key])
    if (count !== undefined) return count
  }
  return undefined
}

function firstCount(...values: unknown[]) {
  return values.map(finiteCount).find((value): value is number => value !== undefined)
}

function parseExecutionGate(value: unknown): FullCycleExecutionGate | undefined {
  if (typeof value === 'string' && value.trim()) return { status: value }
  if (!isRecord(value)) return undefined
  const status = typeof value.status === 'string' ? value.status : typeof value.state === 'string' ? value.state : undefined
  if (!status) return undefined
  return { status, reason: typeof value.reason === 'string' ? value.reason : undefined }
}

function parseFullCycleExecutionSummary(value: unknown): FullCycleExecutionSummary | undefined {
  if (!isRecord(value)) return undefined
  const metadata = isRecord(value.metadata) ? value.metadata : undefined
  const metadataCandidates = value.metadata_candidates ?? value.metadataCandidates
  const metadataJobs = value.metadata_jobs ?? value.metadataJobs
  const candidateCount = firstCount(
    value.metadata_candidates_authorized,
    value.authorized_metadata_candidates,
    value.candidates_authorized,
    value.authorized_candidates,
    countFrom(metadata, ['candidates_authorized', 'authorized_candidates', 'authorized']),
    countFrom(metadataCandidates, ['authorized', 'authorized_count', 'count']),
  )
  const jobsCount = firstCount(
    value.metadata_jobs_queued,
    value.queued_metadata_jobs,
    value.jobs_queued,
    value.queued_jobs,
    countFrom(metadata, ['jobs_queued', 'queued_jobs', 'queued']),
    countFrom(metadataJobs, ['queued', 'queued_count', 'count']),
    countFrom(value.jobs, ['queued', 'queued_jobs', 'queued_count']),
  )
  const summary: FullCycleExecutionSummary = {}
  if (candidateCount !== undefined || jobsCount !== undefined) {
    summary.metadata = { candidates_authorized: candidateCount, jobs_queued: jobsCount }
  }
  if (candidateCount !== undefined) summary.metadata_candidates_authorized = candidateCount
  if (jobsCount !== undefined) summary.metadata_jobs_queued = jobsCount
  const contentPublishing = parseExecutionGate(value.content_publishing ?? value.content_publish ?? value.publishing ?? value.content_publishing_status)
  const paidVisibility = parseExecutionGate(value.paid_visibility ?? value.paid_visibility_status)
  const remoteMutations = parseExecutionGate(value.remote_mutations ?? value.remote_mutation_status)
  if (contentPublishing) summary.content_publishing = contentPublishing
  if (paidVisibility) summary.paid_visibility = paidVisibility
  if (remoteMutations) summary.remote_mutations = remoteMutations
  return summary
}

function parseFullCycleResult(value: Record<string, unknown> | undefined): FullCycleResult | null {
  if (!value || value.workflow !== 'full_cycle') return null

  const stages = (Array.isArray(value.stages) ? value.stages : []).flatMap((item): FullCycleStage[] => {
    if (!isRecord(item) || typeof item.name !== 'string' || typeof item.status !== 'string') return []
    return [{
      name: item.name,
      status: item.status,
      reason: typeof item.reason === 'string' ? item.reason : undefined,
      result: isRecord(item.result) ? item.result : undefined,
    }]
  })
  const nextActions = (Array.isArray(value.next_actions) ? value.next_actions : []).flatMap((item): FullCycleNextAction[] => {
    if (!isRecord(item) || typeof item.action !== 'string' || typeof item.status !== 'string') return []
    return [{
      action: item.action,
      status: item.status,
      reason: typeof item.reason === 'string' ? item.reason : undefined,
      stages: Array.isArray(item.stages) ? item.stages.filter((stage): stage is string => typeof stage === 'string') : undefined,
    }]
  })
  return {
    workflow: 'full_cycle',
    mode: typeof value.mode === 'string' ? value.mode : typeof value.selected_mode === 'string' ? value.selected_mode : undefined,
    complete: value.complete === true,
    stages,
    next_actions: nextActions,
    execution_summary: parseFullCycleExecutionSummary(value.execution_summary ?? value.execution),
  }
}

function siteAutopilotStageLabel(name: string) {
  return SITE_AUTOPILOT_STAGE_LABELS[name] ?? titleCase(name)
}

function siteAutopilotStatus(value: unknown, complete: boolean): SiteAutopilotResult['status'] {
  const status = normalizedStatus(typeof value === 'string' ? value : undefined)
  if (status === 'published') return 'published'
  if (status === 'complete' || (complete && !status)) return 'complete'
  if (['held', 'gated', 'policy_gated', 'needs_connection', 'blocked', 'budget_gated'].includes(status)) return 'gated'
  if (['needs_review', 'review_gated', 'review_needed', 'partial'].includes(status)) return 'needs_review'
  if (['failed', 'error'].includes(status)) return 'failed'
  return complete ? 'complete' : 'unknown'
}

function parseSiteAutopilotResult(value: Record<string, unknown> | undefined): SiteAutopilotResult | null {
  if (!value) return null
  const mode = typeof value.mode === 'string' ? value.mode : ''
  const workflow = typeof value.workflow === 'string' ? value.workflow : ''
  if (mode !== 'autopilot' && workflow !== 'site_autopilot') return null

  const complete = value.complete === true
  const nested = isRecord(value.content_autopilot) ? value.content_autopilot : undefined
  const executionSummary = isRecord(value.execution_summary) ? value.execution_summary : undefined
  const contentPublishing = executionSummary && isRecord(executionSummary.content_publishing)
    ? executionSummary.content_publishing
    : undefined
  const metadata = executionSummary && isRecord(executionSummary.metadata)
    ? executionSummary.metadata
    : undefined
  const candidatesAuthorized = finiteCount(metadata?.authorized_count ?? metadata?.candidates_authorized)
  const jobsQueued = finiteCount(metadata?.queued_count ?? metadata?.jobs_queued)
  const stages = (Array.isArray(value.stages) ? value.stages : []).flatMap((item): Array<{ name: string; status: string }> => {
    if (!isRecord(item) || typeof item.name !== 'string' || typeof item.status !== 'string') return []
    return [{ name: item.name, status: item.status }]
  }).slice(0, 12)
  return {
    status: siteAutopilotStatus(value.status ?? nested?.status ?? contentPublishing?.status, complete),
    complete,
    stages,
    metadata: metadata ? {
      status: typeof metadata.status === 'string' ? metadata.status : undefined,
      candidatesAuthorized,
      jobsQueued,
    } : undefined,
  }
}

function siteAutopilotStatusLabel(status: SiteAutopilotResult['status']) {
  switch (status) {
    case 'complete': return 'Complete'
    case 'published': return 'Published and verified'
    case 'gated': return 'Gated safely'
    case 'needs_review': return 'Needs review'
    case 'failed': return 'Stopped safely'
    default: return 'Needs review'
  }
}

function siteAutopilotStatusTitle(status: SiteAutopilotResult['status']) {
  switch (status) {
    case 'complete': return 'Site autopilot complete'
    case 'published': return 'Site autopilot published and verified'
    case 'gated': return 'Site autopilot is gated'
    case 'needs_review': return 'Site autopilot needs review'
    case 'failed': return 'Site autopilot stopped safely'
    default: return 'Site autopilot result needs review'
  }
}

function siteAutopilotStatusDescription(status: SiteAutopilotResult['status']) {
  switch (status) {
    case 'complete':
    case 'published':
      return 'The bounded workflow finished and the server recorded its verified stages. This does not mean the whole site is optimized.'
    case 'gated':
      return 'The server held this workflow behind a policy, pause, connection, budget, or article prerequisite. No unapproved publication is claimed.'
    case 'needs_review':
      return 'The workflow recorded partial results that need review. No article publication is claimed unless the server explicitly verified it.'
    case 'failed':
      return 'The workflow stopped safely. Review Activity or Incidents for the recorded reason; no unconfirmed publication is claimed.'
    default:
      return 'The server did not provide a complete displayable result. Review Activity before treating this workflow as complete.'
  }
}

function safeAutopilotJobStatus(value: string) {
  switch (normalizedStatus(value)) {
    case 'queued': return 'queued'
    case 'running': return 'running'
    case 'complete': return 'complete'
    case 'partial': return 'partially complete'
    case 'failed': return 'failed'
    case 'blocked': return 'blocked'
    default: return 'in progress'
  }
}

function SiteAutopilotResultsPanel({ result }: { result: SiteAutopilotResult }) {
  const warning = !['complete', 'published'].includes(result.status)
  return <div className="overview-section" style={{ marginTop: 0, marginBottom: 20 }} role="region" aria-labelledby="site-autopilot-results-title" aria-live="polite">
    <Panel padded>
      <div className="panel-header">
        <div>
          <p className="eyebrow">Latest site autopilot</p>
          <h2 className="panel-title" id="site-autopilot-results-title">{siteAutopilotStatusTitle(result.status)}</h2>
          <p className="panel-subtitle">The result below is a bounded workflow summary. It does not expose job identifiers, credentials, operation keys, or provider payloads.</p>
        </div>
        <Badge value={siteAutopilotStatusLabel(result.status)} />
      </div>
      <section aria-labelledby="site-autopilot-stages-title">
        <h3 className="panel-title" id="site-autopilot-stages-title">Workflow stages</h3>
        {result.stages.length ? <ol style={{ margin: 0, paddingLeft: 22 }}>
          {result.stages.map((stage, index) => <li key={`${stage.name}-${index}`} style={{ padding: '10px 0', borderBottom: '1px solid #eeeee9' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
              <strong>{siteAutopilotStageLabel(stage.name)}</strong>
              <Badge value={titleCase(stage.status)} />
            </div>
          </li>)}
        </ol> : <EmptyState title="No stage details were returned" description="The server did not provide stage coverage for this run. Review Activity for the recorded outcome." />}
      </section>
      {result.metadata && <section aria-labelledby="site-autopilot-metadata-title" style={{ marginTop: 18 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
          <div>
            <h3 className="panel-title" id="site-autopilot-metadata-title">Policy-authorized metadata</h3>
            <p className="panel-subtitle">The candidate worker re-checks policy, source freshness, protected resources, and the current remote value before any metadata write.</p>
          </div>
          <Badge value={titleCase(result.metadata.status ?? 'not returned')} />
        </div>
        <dl className="grid-2" style={{ margin: 0 }}>
          <div className="metric-row" style={{ display: 'block', margin: 0 }}><dt className="monitor-label">Candidates authorized</dt><dd className="monitor-value" style={{ margin: '6px 0 0' }}>{result.metadata.candidatesAuthorized === undefined ? 'Not returned' : formatNumber(result.metadata.candidatesAuthorized)}</dd></div>
          <div className="metric-row" style={{ display: 'block', margin: 0 }}><dt className="monitor-label">Jobs queued</dt><dd className="monitor-value" style={{ margin: '6px 0 0' }}>{result.metadata.jobsQueued === undefined ? 'Not returned' : formatNumber(result.metadata.jobsQueued)}</dd></div>
        </dl>
      </section>}
      <div style={{ marginTop: 18 }}><Notice kind={warning ? 'warning' : 'info'} title="What this means">{siteAutopilotStatusDescription(result.status)}</Notice></div>
    </Panel>
  </div>
}

function fullCycleStageLabel(name: string) {
  return FULL_CYCLE_STAGE_LABELS[name] ?? titleCase(name)
}

function fullCycleActionLabel(action: string) {
  return FULL_CYCLE_ACTION_LABELS[action] ?? titleCase(action)
}

function fullCycleStatusLabel(status: string) {
  switch (status) {
    case 'complete': return 'Complete'
    case 'needs_connection': return 'Needs connection'
    case 'needs_review': return 'Needs review'
    case 'partial': return 'Partial / needs review'
    case 'failed': return 'Failed / needs review'
    case 'policy_gated': return 'Policy gated'
    case 'review_gated': return 'Review gated'
    case 'not_run': return 'Not run'
    case 'ready': return 'Ready for review'
    default: return titleCase(status)
  }
}

function fullCycleStageDescription(stage: FullCycleStage) {
  if (stage.status === 'needs_connection') return 'Authenticated WordPress inventory was not run because the connection needs verification.'
  if (stage.status === 'partial') return 'This stage returned partial coverage and needs review before it is treated as complete.'
  if (stage.status === 'failed') return 'This stage could not finish. Review the recorded incident before retrying it.'
  if (stage.status === 'needs_review') return 'This stage finished with work that needs review.'
  if (stage.status === 'complete') return 'The server recorded this stage as complete.'
  return 'The server recorded this stage with an unfinished status.'
}

function fullCycleActionDescription(action: FullCycleNextAction) {
  if (action.action === 'review_incomplete_stages' && action.stages?.length) {
    return `Review: ${action.stages.map(fullCycleStageLabel).join(', ')}.`
  }
  if (action.reason === 'verified_wordpress_connection_required') {
    return 'Verify the WordPress connection before importing authenticated inventory.'
  }
  if (action.reason) return action.reason
  if (action.action === 'review_results') return 'Review the evidence and proposed work before authorizing any governed action.'
  return 'Follow this action from the relevant workspace when you are ready.'
}

type CoverageFreshness = 'fresh' | 'aging' | 'stale' | 'unknown'

function coverageFreshness(lastAuditAt?: string | null): { state: CoverageFreshness; label: string; description: string } {
  if (!lastAuditAt) return { state: 'unknown', label: 'Unknown', description: 'No valid audit timestamp is available.' }
  const timestamp = Date.parse(lastAuditAt)
  if (!Number.isFinite(timestamp)) return { state: 'unknown', label: 'Unknown', description: 'The last audit timestamp is invalid or unavailable.' }
  const ageDays = (Date.now() - timestamp) / (24 * 60 * 60 * 1000)
  if (ageDays < -1) return { state: 'unknown', label: 'Unknown', description: 'The audit timestamp is in the future, so its age cannot be trusted.' }
  if (ageDays <= 7) return { state: 'fresh', label: 'Fresh', description: 'Recorded within the last 7 days.' }
  if (ageDays <= 30) return { state: 'aging', label: 'Aging', description: 'Recorded 8–30 days ago; a new audit is advisable.' }
  return { state: 'stale', label: 'Stale', description: 'Recorded more than 30 days ago; run an audit before relying on this coverage.' }
}

type MonitoringSignalState = 'automatic' | 'needs_review' | 'needs_connection' | 'unsupported' | 'stale' | 'partial_coverage' | 'empty_results' | 'failure'

type MonitoringSignal = {
  label: string
  state: MonitoringSignalState
  description: string
}

function normalizedStatus(value?: string | null) {
  return value?.trim().toLowerCase().replace(/[\s-]+/g, '_') ?? ''
}

function monitoringTone(state: MonitoringSignalState): 'teal' | 'amber' | 'red' | 'slate' | 'blue' | 'green' {
  if (state === 'automatic') return 'green'
  if (state === 'failure' || state === 'unsupported') return 'red'
  if (state === 'empty_results') return 'slate'
  return 'amber'
}

function schedulerSignal(overview: Overview): MonitoringSignal {
  const status = normalizedStatus(overview.monitoring.status)
  if (status === 'unsupported') return { label: 'Scheduler and queue', state: 'unsupported', description: 'The scheduler health signal is not supported by the current deployment.' }
  if (['failed', 'failure', 'error', 'unavailable', 'not_running'].includes(status) || !overview.monitoring.last_seen_at) {
    return { label: 'Scheduler and queue', state: 'failure', description: 'No current scheduler heartbeat is available. Monitoring may not be running, so audits and change checks can be stale.' }
  }
  if (status === 'degraded' || (overview.monitoring.queue_delay_seconds ?? 0) > 0 || (overview.monitoring.missed_checks ?? 0) > 0) {
    return { label: 'Scheduler and queue', state: 'needs_review', description: 'The scheduler is reporting delayed work or missed checks. Review Operations before relying on current coverage.' }
  }
  if (status === 'running' || status === 'healthy') {
    return { label: 'Scheduler and queue', state: 'automatic', description: 'The scheduler heartbeat is current and checks are running automatically. This does not mean the site is optimized.' }
  }
  return { label: 'Scheduler and queue', state: 'needs_review', description: 'The API returned an unrecognized scheduler state. Review Operations before treating monitoring as current.' }
}

function wordpressSignal(overview: Overview): MonitoringSignal {
  const connection = overview.connections.find((item) => normalizedStatus(item.kind) === 'wordpress')
  const connectionStatus = normalizedStatus(connection?.status)
  if (connectionStatus === 'unsupported') return { label: 'WordPress change checks', state: 'unsupported', description: 'This WordPress connection cannot provide the current change-monitoring capability.' }
  if (!connection || ['needs_connection', 'not_connected', 'needs_test', 'disconnected', 'revoked'].includes(connectionStatus)) {
    return { label: 'WordPress change checks', state: 'needs_connection', description: 'Connect and verify WordPress before ForgeSEO can monitor authenticated changes.' }
  }
  if (['failed', 'failure', 'error'].includes(connectionStatus)) return { label: 'WordPress change checks', state: 'failure', description: 'The WordPress connection reported an error. Review the connection before relying on change checks.' }
  if (connectionStatus !== 'connected') return { label: 'WordPress change checks', state: 'needs_review', description: 'The WordPress connection has not reached a verified connected state.' }

  const pollStatus = normalizedStatus(overview.monitoring.wordpress_change_poll?.status)
  if (pollStatus === 'unsupported') return { label: 'WordPress change checks', state: 'unsupported', description: 'The current connection does not support this change-monitoring method.' }
  if (['not_connected', 'needs_connection', 'disconnected', 'revoked'].includes(pollStatus)) return { label: 'WordPress change checks', state: 'needs_connection', description: 'Verify the WordPress connection before authenticated change checks can run.' }
  if (['failed', 'failure', 'error', 'not_running'].includes(pollStatus)) return { label: 'WordPress change checks', state: 'failure', description: 'The change-check service reported a failure. Review the recorded incident before retrying.' }
  if (pollStatus === 'stale' || overview.monitoring.wordpress_change_poll?.missed_window) return { label: 'WordPress change checks', state: 'stale', description: 'The last successful WordPress change check missed its freshness window.' }
  if (pollStatus === 'healthy') return { label: 'WordPress change checks', state: 'automatic', description: 'Verified WordPress changes are being checked automatically.' }
  return { label: 'WordPress change checks', state: 'needs_review', description: 'The first change check has not completed or its current state is unknown.' }
}

function auditCoverageSignal(overview: Overview): MonitoringSignal {
  const status = normalizedStatus(overview.coverage.status)
  if (['failed', 'failure', 'error'].includes(status)) return { label: 'Audit coverage', state: 'failure', description: 'The latest audit reported a failure. Review the recorded job and incident before relying on coverage.' }
  if (['partial', 'partial_coverage', 'complete_with_errors'].includes(status) || (overview.coverage.error_count ?? 0) > 0 || (overview.coverage.pending_url_count ?? 0) > 0) {
    return { label: 'Audit coverage', state: 'partial_coverage', description: 'The latest audit has errors or pending URLs. Findings cover only the evidence that was returned.' }
  }
  if (['not_checked', 'unknown', 'empty', 'no_results'].includes(status) || !overview.coverage.last_audit_at) {
    return { label: 'Audit coverage', state: 'empty_results', description: 'No usable audit result is recorded yet. An empty result is not proof that the site is optimized.' }
  }
  const freshness = coverageFreshness(overview.coverage.last_audit_at)
  if (freshness.state === 'stale') return { label: 'Audit coverage', state: 'stale', description: 'The last audit is more than 30 days old. Run a new audit before relying on this coverage.' }
  if (freshness.state === 'aging' || freshness.state === 'unknown') return { label: 'Audit coverage', state: 'needs_review', description: `${freshness.description} Review the evidence age before treating the audit as current.` }
  if (status === 'complete') return { label: 'Audit coverage', state: 'automatic', description: 'A complete audit result is current within the freshness window. This is coverage evidence, not an optimization score.' }
  return { label: 'Audit coverage', state: 'needs_review', description: 'The API returned an audit state that needs review before it is treated as complete.' }
}

function candidateQueueSignal(overview: Overview): MonitoringSignal {
  if (overview.counts.pending_candidates > 0) {
    return { label: 'Candidate queue', state: 'needs_review', description: `${formatNumber(overview.counts.pending_candidates)} candidate change${overview.counts.pending_candidates === 1 ? '' : 's'} await a decision.` }
  }
  return { label: 'Candidate queue', state: 'empty_results', description: 'No candidate changes are waiting. An empty queue does not mean the site is fully optimized; check audit coverage and findings.' }
}

function visibilitySignal(overview: Overview): MonitoringSignal {
  const providers = overview.connections.filter((item) => ['gsc', 'ga4', 'ai'].includes(normalizedStatus(item.kind)))
  if (!providers.length) return { label: 'Search and AI measurements', state: 'needs_connection', description: 'Connect Search Console, Analytics, or an AI provider to measure search and AI visibility.' }
  const statuses = providers.map((item) => normalizedStatus(item.status))
  if (statuses.every((status) => status === 'unsupported')) return { label: 'Search and AI measurements', state: 'unsupported', description: 'The configured visibility providers do not expose a supported measurement capability.' }
  if (statuses.some((status) => ['failed', 'failure', 'error'].includes(status))) return { label: 'Search and AI measurements', state: 'failure', description: 'A visibility connection reported an error. Review it before relying on measurement results.' }
  if (statuses.some((status) => ['needs_connection', 'not_connected', 'revoked', 'disconnected'].includes(status))) return { label: 'Search and AI measurements', state: 'needs_connection', description: 'One or more visibility providers still need to be connected or reauthorized.' }
  if (statuses.some((status) => status === 'testing' || status === 'needs_test')) return { label: 'Search and AI measurements', state: 'needs_review', description: 'A visibility connection is still being tested and is not yet measurement-ready.' }
  if (statuses.some((status) => status === 'unsupported')) return { label: 'Search and AI measurements', state: 'needs_review', description: 'Some configured visibility sources are unsupported, so measurement coverage is incomplete.' }
  if (statuses.every((status) => status === 'connected')) return { label: 'Search and AI measurements', state: 'automatic', description: 'Configured visibility connections are available for their supported measurements.' }
  return { label: 'Search and AI measurements', state: 'needs_review', description: 'Review the provider states before treating visibility measurements as complete.' }
}

function monitoringSignals(overview: Overview): MonitoringSignal[] {
  return [schedulerSignal(overview), wordpressSignal(overview), auditCoverageSignal(overview), candidateQueueSignal(overview), visibilitySignal(overview)]
}

function MonitoringStatusPanel({ overview }: { overview: Overview }) {
  const signals = monitoringSignals(overview)
  return <div className="overview-section" style={{ marginTop: 0 }} role="region" aria-labelledby="monitoring-status-title">
    <Panel padded>
      <div className="panel-header">
        <div><p className="eyebrow">Evidence status</p><h2 className="panel-title" id="monitoring-status-title">Monitoring and coverage status</h2><p className="panel-subtitle">Each label describes what the API has verified, what needs attention, and what is not connected or supported.</p></div>
      </div>
      <div role="list" aria-label="Monitoring and coverage signals" style={{ display: 'grid', gap: 10 }}>
        {signals.map((signal) => <div key={signal.label} role="listitem" aria-label={`${signal.label}: ${signal.state}`} style={{ display: 'grid', gridTemplateColumns: 'minmax(10rem, 1fr) auto', gap: 10, alignItems: 'start', padding: '12px 0', borderTop: '1px solid #eeeee9' }}>
          <div><strong>{signal.label}</strong><p className="panel-subtitle" style={{ marginTop: 4 }}>{signal.description}</p></div>
          <Badge value={signal.state} tone={monitoringTone(signal.state)} />
        </div>)}
      </div>
      <div style={{ marginTop: 14 }}><Notice kind="info" title="How to read this">Automatic means a check is running or a current result is recorded. Needs review, needs connection, unsupported, stale, partial coverage, empty results, and failure are intentionally different states; none of them is a ranking or optimization score.</Notice></div>
    </Panel>
  </div>
}

function FullCycleResultsPanel({ result }: { result: FullCycleResult }) {
  const isComplete = result.complete === true
  return (
    <div className="overview-section" style={{ marginTop: 0, marginBottom: 20 }} role="region" aria-labelledby="full-cycle-results-title" aria-live="polite">
      <Panel padded>
        <div className="panel-header">
          <div>
            <p className="eyebrow">Latest full cycle</p>
            <h2 className="panel-title" id="full-cycle-results-title">What ForgeSEO found and what happens next</h2>
            <p className="panel-subtitle">This result stays visible after the job finishes so your team can act on the recorded coverage.</p>
          </div>
          <Badge value={isComplete ? 'Complete' : 'Partial / needs review'} />
        </div>
        <div className="grid-2">
          <section aria-labelledby="full-cycle-stages-title">
            <h3 className="panel-title" id="full-cycle-stages-title">Stages</h3>
            {result.stages.length ? <ol style={{ margin: 0, paddingLeft: 22 }}>
              {result.stages.map((stage, index) => <li key={`${stage.name}-${index}`} style={{ padding: '10px 0', borderBottom: '1px solid #eeeee9' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
                  <strong>{fullCycleStageLabel(stage.name)}</strong>
                  <Badge value={fullCycleStatusLabel(stage.status)} />
                </div>
                <p className="panel-subtitle" style={{ marginTop: 5 }}>{fullCycleStageDescription(stage)}</p>
              </li>)}
            </ol> : <EmptyState title="No stages were returned" description="The server did not provide stage coverage for this result. Review Activity for the recorded job outcome." />}
          </section>
          <section aria-labelledby="full-cycle-actions-title">
            <h3 className="panel-title" id="full-cycle-actions-title">Next actions</h3>
            {result.next_actions.length ? <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
              {result.next_actions.map((action, index) => <li key={`${action.action}-${index}`} style={{ padding: '10px 0', borderBottom: '1px solid #eeeee9' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 10, flexWrap: 'wrap' }}>
                  <strong>{fullCycleActionLabel(action.action)}</strong>
                  <Badge value={fullCycleStatusLabel(action.status)} />
                </div>
                <p className="panel-subtitle" style={{ marginTop: 5 }}>{fullCycleActionDescription(action)}</p>
              </li>)}
            </ul> : <EmptyState title="No follow-ups were returned" description="There are no server-recorded next actions for this result." />}
          </section>
        </div>
        <div style={{ marginTop: 18 }}><Notice kind="info" title="Governed follow-ups">The full cycle is read-only. Writes and paid services remain governed by the active policy and required connections.</Notice></div>
      </Panel>
    </div>
  )
}

type GovernedCycleReadiness = {
  canRun: boolean
  kind: 'info' | 'warning'
  title: string
  message: string
  canRetry: boolean
}

type PolicySnapshot = {
  enabled?: boolean
  allowedActions?: string[]
  status?: string
}

function policySnapshot(overview: Overview, policySettings?: Record<string, unknown>): PolicySnapshot | null {
  const embedded = overview.policy_state ?? overview.policy
  const source: unknown = (embedded && isRecord(embedded) && isRecord(embedded.settings))
    ? embedded.settings
    : embedded ?? policySettings
  if (!isRecord(source)) return null
  return {
    enabled: typeof source.enabled === 'boolean' ? source.enabled : undefined,
    allowedActions: Array.isArray(source.allowed_actions) ? source.allowed_actions.filter((action): action is string => typeof action === 'string') : undefined,
    status: typeof source.status === 'string' ? source.status : undefined,
  }
}

function governedCycleReadiness({
  overview,
  role,
  policySettings,
  policyLoading,
  policyError,
  globalPause,
  globalPauseLoading,
  globalPauseError,
}: {
  overview: Overview
  role: 'owner' | 'editor' | 'viewer' | null
  policySettings?: Record<string, unknown>
  policyLoading: boolean
  policyError: string | null
  globalPause?: boolean
  globalPauseLoading: boolean
  globalPauseError: string | null
}): GovernedCycleReadiness {
  if (role === 'viewer') return { canRun: false, kind: 'warning', title: 'Read-only for your role', message: 'Viewer role cannot start a governed cycle. Ask an owner or editor to review and run policy-approved metadata work.', canRetry: false }
  if (!role) return { canRun: false, kind: 'warning', title: 'Role is not available', message: 'Your workspace role could not be verified. Refresh the overview before starting governed work.', canRetry: true }
  if (overview.site.paused) return { canRun: false, kind: 'warning', title: 'Governed cycle paused', message: 'This site is paused. Review the site policy controls before running policy-approved metadata actions.', canRetry: false }
  if (globalPause === true) return { canRun: false, kind: 'warning', title: 'Workspace pause is active', message: 'The workspace emergency pause is active. Governed execution is held until an owner resumes it.', canRetry: false }
  if (globalPause === undefined) {
    if (globalPauseLoading) return { canRun: false, kind: 'info', title: 'Checking workspace pause', message: 'The governed cycle will remain unavailable until the current workspace pause state is verified.', canRetry: false }
    return { canRun: false, kind: 'warning', title: 'Workspace pause state unavailable', message: globalPauseError ? 'The workspace pause state could not be verified. Retry the governed checks before running policy-approved actions.' : 'The workspace pause state was not returned. Retry the governed checks before running policy-approved actions.', canRetry: true }
  }

  const policy = policySnapshot(overview, policySettings)
  if (!policy) {
    if (policyLoading) return { canRun: false, kind: 'info', title: 'Checking site policy', message: 'The governed cycle is disabled until the current policy and metadata authorization are verified.', canRetry: false }
    return { canRun: false, kind: 'warning', title: 'Site policy state unavailable', message: policyError ? 'The site policy could not be verified. Retry the governed checks before running policy-approved actions.' : 'The site policy was not returned. Retry the governed checks before running policy-approved actions.', canRetry: true }
  }
  if (policy.enabled === false || ['disabled', 'paused', 'blocked', 'not_ready', 'not_configured'].includes(normalizedStatus(policy.status))) {
    return { canRun: false, kind: 'warning', title: 'Governed cycle is disabled', message: 'Site policy does not currently enable governed execution. Review policy controls before running metadata actions.', canRetry: false }
  }
  const allowedActions = policy.allowedActions?.map(normalizedStatus)
  if (!allowedActions?.length || (!allowedActions.includes('metadata') && !allowedActions.includes('metadata_writes'))) {
    return { canRun: false, kind: 'warning', title: 'Metadata actions are not approved', message: 'The current site policy does not approve metadata actions. Review allowed actions before running a governed cycle.', canRetry: false }
  }
  if (policy.enabled !== true) {
    return { canRun: false, kind: 'warning', title: 'Site policy is not ready', message: 'The current policy did not confirm that governed execution is enabled. Review policy controls before running metadata actions.', canRetry: false }
  }
  return { canRun: true, kind: 'info', title: 'Governed cycle is ready', message: 'Current policy approves metadata actions. The server will re-check policy and pause state when the job starts.', canRetry: false }
}

function siteAutopilotReadiness({
  overview,
  role,
  policySettings,
  policyLoading,
  policyError,
  globalPause,
  globalPauseLoading,
  globalPauseError,
}: {
  overview: Overview
  role: 'owner' | 'editor' | 'viewer' | null
  policySettings?: Record<string, unknown>
  policyLoading: boolean
  policyError: string | null
  globalPause?: boolean
  globalPauseLoading: boolean
  globalPauseError: string | null
}): SiteAutopilotReadiness {
  if (!role) return { canRun: false, kind: 'warning', title: 'Owner role is not verified', message: 'Site autopilot is unavailable until the current workspace role is verified. Refresh the overview before starting it.', canRetry: true }
  if (role !== 'owner') return { canRun: false, kind: 'warning', title: 'Owner access required', message: 'Editor and Viewer roles can review this workflow, but only the site owner can run site autopilot.', canRetry: false }
  if (overview.site.paused) return { canRun: false, kind: 'warning', title: 'Site autopilot is paused', message: 'Site autopilot is unavailable while site-level automation is paused. Resume it in the policy controls only after the pilot safeguards are ready.', canRetry: false }
  if (globalPause === true) return { canRun: false, kind: 'warning', title: 'Workspace pause is active', message: 'A workspace-wide emergency stop currently holds site autopilot. An owner must resume it before starting this workflow.', canRetry: false }
  if (globalPause === undefined) {
    if (globalPauseLoading) return { canRun: false, kind: 'info', title: 'Checking workspace pause', message: 'Site autopilot will remain unavailable until the current workspace pause state is verified.', canRetry: false }
    return { canRun: false, kind: 'warning', title: 'Workspace pause state unavailable', message: globalPauseError ? 'The workspace pause state could not be verified. Retry readiness checks before starting site autopilot.' : 'The workspace pause state was not returned. Retry readiness checks before starting site autopilot.', canRetry: true }
  }
  if (policyLoading) return { canRun: false, kind: 'info', title: 'Checking site readiness', message: 'Site autopilot will remain unavailable until the current policy and budget controls are verified.', canRetry: false }
  if (policyError || !policySettings) return { canRun: false, kind: 'warning', title: 'Site readiness is unavailable', message: 'The current site policy and budget controls could not be verified. Retry readiness checks before starting site autopilot.', canRetry: true }
  if (!overview.budget || !Array.isArray(overview.connections)) return { canRun: false, kind: 'warning', title: 'Site readiness is incomplete', message: 'The overview did not return the budget and connection state needed to start site autopilot safely. Refresh the overview and retry.', canRetry: true }
  return { canRun: true, kind: 'info', title: 'Site autopilot is ready to check', message: 'The owner can start the bounded workflow. The server will re-check policy, pauses, WordPress access, article prerequisites, and budget before any publication.', canRetry: false }
}

function GovernedCycleAvailability({ readiness, onRetry }: { readiness: GovernedCycleReadiness; onRetry: () => void }) {
  return <div id="governed-cycle-gate" className="mb-20"><Notice kind={readiness.kind} title={readiness.title}>{readiness.message}{readiness.canRetry && <div style={{ marginTop: 9 }}><Button variant="secondary" size="sm" onClick={onRetry}>Retry governed checks</Button></div>}</Notice></div>
}

function SiteAutopilotAvailability({ readiness, onRetry }: { readiness: SiteAutopilotReadiness; onRetry: () => void }) {
  return <div id="site-autopilot-gate" className="mb-20"><Notice kind={readiness.kind} title={readiness.title}>{readiness.message}{readiness.canRetry && <div style={{ marginTop: 9 }}><Button variant="secondary" size="sm" onClick={onRetry}>Retry site autopilot readiness</Button></div>}</Notice></div>
}

function governedGateDescription(kind: 'content' | 'paid' | 'remote', gate: FullCycleExecutionGate) {
  if (gate.reason) return gate.reason
  if (kind === 'content') return 'Articles were not published; content publishing remains behind explicit review.'
  if (kind === 'paid') return 'Paid visibility was not run, so no provider spend was initiated.'
  return 'Other remote mutations were not run by this governed cycle.'
}

function GovernedCycleResultsPanel({ result }: { result: FullCycleResult }) {
  const summary = result.execution_summary
  const candidates = summary?.metadata?.candidates_authorized ?? summary?.metadata_candidates_authorized
  const jobs = summary?.metadata?.jobs_queued ?? summary?.metadata_jobs_queued
  const gates: Array<{ key: string; label: string; kind: 'content' | 'paid' | 'remote'; gate: FullCycleExecutionGate }> = [
    { key: 'content-publishing', label: 'Content publishing', kind: 'content', gate: summary?.content_publishing ?? { status: 'review_gated' } },
    { key: 'paid-visibility', label: 'Paid visibility', kind: 'paid', gate: summary?.paid_visibility ?? { status: 'not_run' } },
    { key: 'remote-mutations', label: 'Other remote mutations', kind: 'remote', gate: summary?.remote_mutations ?? { status: 'not_run' } },
  ]
  return <div className="overview-section" style={{ marginTop: 0, marginBottom: 20 }} role="region" aria-labelledby="governed-cycle-results-title" aria-live="polite">
    <Panel padded>
      <div className="panel-header">
        <div>
          <p className="eyebrow">Latest governed cycle</p>
          <h2 className="panel-title" id="governed-cycle-results-title">Policy-authorized execution summary</h2>
          <p className="panel-subtitle">Selected mode: <strong>Governed</strong>. This records what the site policy authorized and queued; it is not an optimization or ranking result.</p>
        </div>
        <Badge value={result.complete === true ? 'Complete' : 'Partial / needs review'} />
      </div>
      <dl className="grid-2" aria-label="Governed execution totals" style={{ margin: 0 }}>
        <div className="metric-row" style={{ display: 'block', margin: 0 }}><dt className="monitor-label">Metadata candidates authorized</dt><dd className="monitor-value" style={{ margin: '6px 0 0' }}>{candidates === undefined ? 'Not returned' : formatNumber(candidates)}</dd><p className="panel-subtitle" style={{ marginTop: 3 }}>Candidates allowed by the current policy.</p></div>
        <div className="metric-row" style={{ display: 'block', margin: 0 }}><dt className="monitor-label">Metadata jobs queued</dt><dd className="monitor-value" style={{ margin: '6px 0 0' }}>{jobs === undefined ? 'Not returned' : formatNumber(jobs)}</dd><p className="panel-subtitle" style={{ marginTop: 3 }}>Jobs recorded for the authorized metadata work.</p></div>
      </dl>
      <section aria-labelledby="governed-cycle-boundaries-title" style={{ marginTop: 18 }}>
        <h3 className="panel-title" id="governed-cycle-boundaries-title">Explicit review boundaries</h3>
        <div role="list" aria-label="Governed cycle review boundaries" style={{ display: 'grid', gap: 10 }}>
          {gates.map(({ key, label, kind, gate }) => <div key={key} role="listitem" style={{ display: 'grid', gridTemplateColumns: 'minmax(10rem, 1fr) auto', gap: 10, alignItems: 'start', padding: '10px 0', borderTop: '1px solid #eeeee9' }}><div><strong>{label}</strong><p className="panel-subtitle" style={{ marginTop: 4 }}>{governedGateDescription(kind, gate)}</p></div><Badge value={fullCycleStatusLabel(gate.status)} /></div>)}
        </div>
      </section>
      <div style={{ marginTop: 18 }}><Notice kind="info" title="Read the completion status carefully">A complete governed-cycle execution only means the recorded policy-authorized job finished. It does not mean the audit is complete, the site is optimized, articles were published, or providers were charged.</Notice></div>
    </Panel>
  </div>
}

export function OverviewPage() {
  const siteId = useSiteId()
  const { role } = useAuth()
  const [actionMessage, setActionMessage] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [fullCycleResult, setFullCycleResult] = useState<FullCycleResult | null>(null)
  const [governedCycleResult, setGovernedCycleResult] = useState<FullCycleResult | null>(null)
  const [siteAutopilotResult, setSiteAutopilotResult] = useState<SiteAutopilotResult | null>(null)
  const [runningAction, setRunningAction] = useState<'audit' | 'full_cycle' | 'governed_cycle' | 'autopilot' | null>(null)
  const loader = useCallback(() => sitesApi.overview(siteId), [siteId])
  const resource = useResource(loader, [siteId])
  const policyLoader = useCallback(() => policyApi.get(siteId), [siteId])
  const policyResource = useResource(policyLoader, [siteId])
  const settingsLoader = useCallback(() => settingsApi.get(), [])
  const settingsResource = useResource(settingsLoader, [])

  async function runAudit() {
    setActionMessage(null)
    setActionError(null)
    setRunningAction('audit')
    try {
      const job = await jobsApi.create(siteId, { kind: 'audit', payload: {}, idempotency_key: `audit-${Date.now()}` })
      const finished = await jobsApi.wait(siteId, job.id, { onUpdate: (next) => setActionMessage(`Audit is ${next.status}. The server is collecting evidence; this page will refresh when it records coverage.`) })
      setActionMessage(finished.status === 'complete' || finished.status === 'partial'
        ? `Audit ${finished.status}. Coverage and findings have been refreshed from the server.`
        : `Audit is ${finished.status}. Open Activity or Incidents for the server's recorded reason.`)
      await resource.reload()
    } catch (error) {
      setActionError(detailMessage(error))
    } finally {
      setRunningAction(null)
    }
  }

  async function runCycle(mode: 'read_only' | 'governed') {
    setActionMessage(null)
    setActionError(null)
    const action = mode === 'governed' ? 'governed_cycle' : 'full_cycle'
    const label = mode === 'governed' ? 'Governed cycle' : 'Full cycle'
    setRunningAction(action)
    try {
      const job = await jobsApi.create(siteId, { kind: 'full_cycle', payload: mode === 'governed' ? { mode: 'governed' } : {}, idempotency_key: `${mode === 'governed' ? 'governed-' : ''}full-cycle-${Date.now()}` })
      const finished = await jobsApi.wait(siteId, job.id, {
        onUpdate: (next) => setActionMessage(`${label} is ${next.status}. ${mode === 'governed' ? 'Only policy-approved metadata actions can be activated; publishing and paid visibility remain review-gated.' : FULL_CYCLE_SCOPE}`),
      })
      const parsedResult = parseFullCycleResult(finished.result)
      if (mode === 'governed') setGovernedCycleResult(parsedResult)
      else setFullCycleResult(parsedResult)
      setActionMessage(finished.status === 'complete' || finished.status === 'partial'
        ? mode === 'governed'
          ? `Governed cycle ${finished.status}. Policy-authorized metadata actions are summarized below; publishing, paid visibility, and other remote mutations were not run.`
          : `Full cycle ${finished.status}. The overview and recorded coverage have been refreshed. Writes and paid services remain policy/connection-gated.`
        : `${label} is ${finished.status}. Open Activity or Incidents for the server's recorded reason.`)
      await resource.reload()
    } catch (error) {
      setActionError(detailMessage(error))
    } finally {
      setRunningAction(null)
    }
  }

  async function runFullCycle() {
    await runCycle('read_only')
  }

  async function runGovernedCycle() {
    await runCycle('governed')
  }

  async function runSiteAutopilot() {
    setActionMessage(null)
    setActionError(null)
    setSiteAutopilotResult(null)
    setRunningAction('autopilot')
    try {
      const job = await jobsApi.create(siteId, {
        kind: 'full_cycle',
        payload: { mode: 'autopilot' },
        idempotency_key: `site-autopilot-${Date.now()}`,
      })
      const finished = await jobsApi.wait(siteId, job.id, {
        onUpdate: (next) => setActionMessage(`Site autopilot is ${safeAutopilotJobStatus(next.status)}. It is checking the bounded workflow; any publication remains prerequisite-gated.`),
      })
      const parsedResult = parseSiteAutopilotResult(finished.result)
      if (parsedResult) setSiteAutopilotResult(parsedResult)
      const status = parsedResult?.status
      if (status === 'gated') {
        setActionMessage('Site autopilot was held safely by a prerequisite gate. No unapproved publication was claimed.')
      } else if (status === 'needs_review') {
        setActionMessage('Site autopilot recorded partial work and needs review. No unverified publication was claimed.')
      } else if (status === 'failed') {
        setActionMessage('Site autopilot stopped safely. Review the recorded incident before trying again.')
      } else if (status === 'complete' || status === 'published') {
        setActionMessage('Site autopilot completed its bounded workflow. Review the stage summary below; this is not proof that the whole site is optimized.')
      } else {
        setActionMessage('Site autopilot finished without a displayable stage summary. Review Activity before treating the run as complete.')
      }
      await resource.reload()
    } catch (error) {
      setActionError(detailMessage(error))
    } finally {
      setRunningAction(null)
    }
  }

  return <ResourceStateView resource={resource} empty={<ErrorState message="The site overview was empty." onRetry={() => void resource.reload()} />}>
    {(overview) => {
      const freshness = coverageFreshness(overview.coverage.last_audit_at)
      const globalPause = typeof overview.global_pause === 'boolean' ? overview.global_pause : settingsResource.data?.global_pause
      const readiness = governedCycleReadiness({
        overview,
        role,
        policySettings: policyResource.data?.settings,
        policyLoading: policyResource.loading,
        policyError: policyResource.error,
        globalPause,
        globalPauseLoading: settingsResource.loading,
        globalPauseError: settingsResource.error,
      })
      const autopilotReadiness = siteAutopilotReadiness({
        overview,
        role,
        policySettings: policyResource.data?.settings,
        policyLoading: policyResource.loading,
        policyError: policyResource.error,
        globalPause,
        globalPauseLoading: settingsResource.loading,
        globalPauseError: settingsResource.error,
      })
      const automationState = overview.site.paused
        ? { label: 'Paused', description: 'Site-level automation is paused. Observations and reviews remain available; governed writes are held.' }
        : { label: 'Not paused', description: 'Site-level pause is off. Workflows still follow policy and connection gates.' }
      return <>
      <PageHeader eyebrow="Site overview" title={overview.site.name} description={`${overview.site.origin.replace(/^https?:\/\//, '')} · ${overview.site.timezone}`} actions={<><Button variant="secondary" onClick={() => void resource.reload()} disabled={resource.loading}><RefreshCw size={15} /> Refresh</Button><Button variant="secondary" onClick={() => void runAudit()} disabled={runningAction !== null} aria-busy={runningAction === 'audit'}><Play size={15} /> {runningAction === 'audit' ? 'Starting…' : 'Run audit'}</Button><Button variant="secondary" onClick={() => void runGovernedCycle()} disabled={runningAction !== null || !readiness.canRun} aria-busy={runningAction === 'governed_cycle'} aria-describedby="governed-cycle-help governed-cycle-gate"><ShieldCheck size={15} /> {runningAction === 'governed_cycle' ? 'Running governed cycle…' : 'Run governed cycle'}</Button><Button variant="secondary" onClick={() => void runSiteAutopilot()} disabled={runningAction !== null || !autopilotReadiness.canRun} aria-busy={runningAction === 'autopilot'} aria-describedby="site-autopilot-help site-autopilot-gate"><Sparkles size={15} /> {runningAction === 'autopilot' ? 'Running site autopilot…' : 'Run site autopilot'}</Button><Button onClick={() => void runFullCycle()} disabled={runningAction !== null} aria-busy={runningAction === 'full_cycle'} aria-describedby="full-cycle-help"><Sparkles size={15} /> {runningAction === 'full_cycle' ? 'Running…' : 'Run full cycle'}</Button></>} />
      <div id="full-cycle-help" className="mb-20"><Notice kind="info" title="Full cycle">This command runs availability, inventory/audit, content planning, and refresh evaluation in one authenticated job. Writes and paid services remain policy/connection-gated.</Notice></div>
      <div id="governed-cycle-help" className="mb-20"><Notice kind="info" title="Governed cycle">This command only activates site policy-approved metadata actions. It does not silently publish articles, spend on providers, or perform other remote mutations; those remain review-gated or not run.</Notice></div>
      <div id="site-autopilot-help" className="mb-20"><Notice kind="info" title="Site autopilot">{SITE_AUTOPILOT_SCOPE}</Notice></div>
      <GovernedCycleAvailability readiness={readiness} onRetry={() => { void Promise.all([policyResource.reload(), settingsResource.reload()]) }} />
      <SiteAutopilotAvailability readiness={autopilotReadiness} onRetry={() => { void Promise.all([resource.reload(), policyResource.reload(), settingsResource.reload()]) }} />
      {actionMessage && <div className="mb-20"><Notice kind="success">{actionMessage}</Notice></div>}
      {actionError && <div className="mb-20"><Notice kind="error">{actionError}</Notice></div>}
      {governedCycleResult && <GovernedCycleResultsPanel result={governedCycleResult} />}
      {siteAutopilotResult && <SiteAutopilotResultsPanel result={siteAutopilotResult} />}
      {fullCycleResult && <FullCycleResultsPanel result={fullCycleResult} />}
      <div className="hero-strip">
        <div className="welcome-card"><p className="eyebrow">The careful view</p><h2>{overview.site.paused ? 'Automation is paused while you review the map.' : 'Review monitoring health and automation activity.'}</h2><p>{overview.site.paused ? 'Findings and candidate changes can still be collected. Publishing remains behind the controls in Policies & budget.' : 'Keep an eye on signal quality, candidates, and the work your team has approved.'}</p><Link to={`/sites/${siteId}/settings/policies`}>{overview.site.paused ? 'Review policy controls' : 'Open policy controls'} <ArrowUpRight size={14} style={{ verticalAlign: 'middle' }} /></Link></div>
        <div className="monitor-card"><div className="monitor-card-top"><div><div className="monitor-label">Monitoring</div><div className="monitor-value">{overview.monitoring.status === 'healthy' || overview.monitoring.status === 'running' ? 'All systems steady' : titleCase(overview.monitoring.status)}</div><div className="monitor-meta">Last seen {formatDateTime(overview.monitoring.last_seen_at)}</div></div><Gauge size={25} color="#148b89" /></div><div><div className="budget-label-row" role="status" aria-label={`Automation state: ${automationState.label}`}><span>Automation</span><strong>{automationState.label}</strong></div><div className="monitor-meta" style={{ marginTop: 7 }}>{automationState.description}</div><div className="budget-label-row" style={{ marginTop: 14 }}><span>Coverage</span><strong>{titleCase(overview.coverage.status)}</strong></div><div style={{ marginTop: 8 }}><span className="text-small text-muted">Coverage is based on the last recorded audit, not an optimization score.</span></div>{((overview.coverage.error_count ?? 0) > 0 || (overview.coverage.pending_url_count ?? 0) > 0) && <div className="monitor-meta" style={{ marginTop: 7, color: '#a45d16' }}>{overview.coverage.error_count ?? 0} audit errors · {overview.coverage.pending_url_count ?? 0} URLs still pending</div>}<div className="monitor-meta" style={{ marginTop: 7 }}>Last audit {formatDateTime(overview.coverage.last_audit_at)}</div><div className="monitor-meta" style={{ marginTop: 7 }}>Queue delay {overview.monitoring.queue_delay_seconds ?? 'unknown'}s · missed checks {overview.monitoring.missed_checks ?? 'unknown'}</div>{overview.monitoring.wordpress_change_poll && <div className="monitor-meta" style={{ marginTop: 7 }}>WordPress changes: {titleCase(overview.monitoring.wordpress_change_poll.status)} · last successful poll {formatDateTime(overview.monitoring.wordpress_change_poll.last_success_at, 'Not yet run')}</div>}</div></div>
      </div>
      {(overview.monitoring.status === 'not_running' || !overview.monitoring.last_seen_at || overview.monitoring.status === 'degraded' || (overview.monitoring.queue_delay_seconds ?? 0) > 0 || (overview.monitoring.missed_checks ?? 0) > 0 || overview.monitoring.wordpress_change_poll?.status === 'stale' || overview.monitoring.wordpress_change_poll?.missed_window) && <div className="mb-20"><Notice kind="warning" title={overview.monitoring.status === 'not_running' || !overview.monitoring.last_seen_at ? 'Monitoring is not running' : undefined}>{overview.monitoring.status === 'not_running' || !overview.monitoring.last_seen_at ? 'Monitoring has not reported a heartbeat yet. Audits and change checks may be stale until the scheduler starts.' : overview.monitoring.wordpress_change_poll?.status === 'stale' || overview.monitoring.wordpress_change_poll?.missed_window ? (overview.monitoring.wordpress_change_poll.message ?? 'WordPress change monitoring is stale. Coverage may be delayed until polling recovers.') : `Monitoring status is ${overview.monitoring.status}. Queue delay: ${overview.monitoring.queue_delay_seconds ?? 'unknown'} seconds; missed checks: ${overview.monitoring.missed_checks ?? 'unknown'}. Coverage may be stale until the scheduler catches up.`}</Notice></div>}
      <div className="mb-20" role="status" aria-label={`Audit coverage freshness: ${freshness.label}`}><Notice kind={freshness.state === 'stale' || freshness.state === 'unknown' ? 'warning' : 'info'} title={`Audit data: ${freshness.label}`}>{freshness.description} This is an evidence-age warning, not a ranking or optimization score.</Notice></div>
      <MonitoringStatusPanel overview={overview} />
      <div className="grid-4">
        <StatCard label="Pages in inventory" value={formatNumber(overview.counts.pages)} detail="Discovered page records" icon={<FileText size={15} />} />
        <StatCard label="Open issues" value={formatNumber(overview.counts.open_findings)} detail="Findings needing review" icon={<AlertTriangle size={15} />} tone={overview.counts.open_findings ? 'amber' : 'teal'} />
        <StatCard label="Candidate changes" value={formatNumber(overview.counts.pending_candidates)} detail="Waiting for a decision" icon={<Sparkles size={15} />} tone={overview.counts.pending_candidates ? 'amber' : 'teal'} />
        <StatCard label="Published articles" value={formatNumber(overview.counts.published_articles)} detail="Recorded by the API" icon={<BookOpen size={15} />} tone="navy" />
      </div>
      <div className="grid-2 overview-section">
        <Panel>
          <div className="panel-header" style={{ padding: '22px 22px 0' }}><div><h2 className="panel-title">Recent activity</h2><p className="panel-subtitle">A compact trail of changes and observations.</p></div><Link to={`/sites/${siteId}/activity`} className="link-button">View all</Link></div>
          {overview.recent_events.length ? <div className="event-list" style={{ padding: '0 22px 12px' }}>{overview.recent_events.slice(0, 6).map((event) => <div className="event-row" key={String(event.id)}><span className="event-dot" /><div><div className="event-kind">{event.kind}</div><div className="event-message">{event.message}</div></div><time className="event-time">{formatDateTime(event.created_at)}</time></div>)}</div> : <EmptyState icon={<Activity size={20} />} title="No activity yet" description="Events will appear here after your first inventory, audit, or editorial decision." />}
        </Panel>
        <Panel padded>
          <div className="panel-header"><div><h2 className="panel-title">Budget this month</h2><p className="panel-subtitle">Reserved and spent amounts come from the API budget ledger.</p></div><Link to={`/sites/${siteId}/settings/policies`} className="link-button">Manage</Link></div>
          <div className="budget-card"><div className="budget-label-row"><span>Spent + reserved</span><strong>{formatCurrencyCents(overview.budget.spent_cents + overview.budget.reserved_cents)} / {formatCurrencyCents(overview.budget.limit_cents)}</strong></div><ProgressBar value={percent(overview.budget.spent_cents + overview.budget.reserved_cents, overview.budget.limit_cents)} tone={percent(overview.budget.spent_cents + overview.budget.reserved_cents, overview.budget.limit_cents) > 85 ? 'red' : 'teal'} /><div className="metric-row"><span>Spent</span><strong>{formatCurrencyCents(overview.budget.spent_cents)}</strong></div><div className="metric-row"><span>Reserved</span><strong>{formatCurrencyCents(overview.budget.reserved_cents)}</strong></div></div>
          <div className="divider" /><div className="notice notice-info"><ShieldCheck size={16} /><div>Actions remain subject to the active policy, protected paths, and the global pause.</div></div>
        </Panel>
      </div>
      <div className="overview-section"><div className="section-heading"><h2>Connections</h2><Link to={`/sites/${siteId}/settings/connections`}>Manage connections <ArrowUpRight size={13} style={{ verticalAlign: 'middle' }} /></Link></div><div className="connection-list">{overview.connections.length ? overview.connections.map((connection) => <div className="connection-chip" key={connection.kind}><div><div className="connection-chip-name">{connection.kind === 'gsc' ? 'Search Console' : connection.kind === 'ga4' ? 'Google Analytics' : connection.kind === 'ai' ? 'AI provider' : connection.kind}</div><div className="connection-chip-status">{connection.checked_at ? `Checked ${formatDateTime(connection.checked_at)}` : 'No check recorded'}</div></div><Badge value={connection.status} /></div>) : <Panel><EmptyState icon={<PlugIcon />} title="No connections yet" description="Add the source your next workflow needs. ForgeSEO will keep the rest in a needs-connection state." action={<Link to={`/sites/${siteId}/settings/connections`} className="button button-secondary button-sm">Open connections</Link>} /></Panel>}</div></div>
      </>
    }}
  </ResourceStateView>
}

function PlugIcon() {
  return <span style={{ fontSize: '1.15rem' }}>↗</span>
}
