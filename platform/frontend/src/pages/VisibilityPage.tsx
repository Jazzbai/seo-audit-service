import { useCallback, useState } from 'react'
import { ArrowUpRight, Database, Download, Eye, FileInput, RefreshCw, Search, Settings2, UploadCloud } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Badge, Button, EmptyState, ErrorState, Field, Notice, PageHeader, Panel, TableShell } from '../components/ui'
import { connectionsApi, detailMessage, jobsApi, operationsApi, policyApi } from '../lib/api'
import { formatDateTime, titleCase, truncate } from '../lib/format'
import type { Connection, Measurement } from '../types'
import { ResourceStateView, useResource, useSiteId } from './shared'

type VisibilityTab = 'observed' | 'imports' | 'settings'
type VisibilitySourceKind = 'gsc' | 'ga4' | 'pagespeed' | 'dataforseo' | 'ai_sample'
type ObservationBasis = 'lab' | 'field' | 'field_performance' | 'lab_and_field' | 'unknown'

type ObservationCategory = {
  key: ObservationBasis
  label: string
  description: string
  tone: 'amber' | 'blue' | 'green' | 'teal'
}

type ObservationFreshness = 'fresh' | 'aging' | 'stale' | 'unknown'

type ObservationFreshnessInfo = {
  key: ObservationFreshness
  label: string
  description: string
  tone: 'green' | 'amber' | 'red' | 'slate'
}

type VisibilitySourceStateKey = 'ready' | 'needs_connection' | 'error' | 'stale' | 'empty' | 'needs_review' | 'unsupported'

type VisibilitySourceState = {
  key: VisibilitySourceStateKey
  label: string
  description: string
  tone: 'green' | 'amber' | 'red' | 'slate'
}

const visibilitySources: Array<{ kind: string; label: string }> = [
  { kind: 'gsc', label: 'Google Search Console' },
  { kind: 'ga4', label: 'Google Analytics 4' },
  { kind: 'dataforseo', label: 'DataForSEO' },
  { kind: 'ai', label: 'AI sample provider' },
  { kind: 'pagespeed', label: 'PageSpeed' },
]

const observationCategories: Record<ObservationBasis, ObservationCategory> = {
  lab: {
    key: 'lab',
    label: 'Lab Performance',
    description: 'Controlled PageSpeed or Lighthouse measurement; it is not field-user experience.',
    tone: 'blue',
  },
  field: {
    key: 'field',
    label: 'Field / Traffic',
    description: 'Observed search or traffic activity from GSC, GA4, or referral data.',
    tone: 'green',
  },
  field_performance: {
    key: 'field_performance',
    label: 'Field Performance',
    description: 'Available real-user or origin loading-experience data; it is separate from controlled lab measurements.',
    tone: 'teal',
  },
  lab_and_field: {
    key: 'lab_and_field',
    label: 'Lab + Field Performance',
    description: 'The observation includes controlled lab data and available field or origin data; keep the two contexts separate when comparing results.',
    tone: 'teal',
  },
  unknown: {
    key: 'unknown',
    label: 'Unknown / Imported',
    description: 'Imported or unclassified evidence; confirm its provenance before relying on it.',
    tone: 'amber',
  },
}

const observationFreshness: Record<ObservationFreshness, ObservationFreshnessInfo> = {
  fresh: {
    key: 'fresh',
    label: 'Fresh',
    description: 'Observed within the last 7 days.',
    tone: 'green',
  },
  aging: {
    key: 'aging',
    label: 'Aging',
    description: 'Observed more than 7 and up to 30 days ago.',
    tone: 'amber',
  },
  stale: {
    key: 'stale',
    label: 'Stale',
    description: 'Observed more than 30 days ago; collect a newer observation before treating it as current.',
    tone: 'red',
  },
  unknown: {
    key: 'unknown',
    label: 'Unknown',
    description: 'The timestamp is missing, invalid, or in the future, so freshness cannot be determined.',
    tone: 'slate',
  },
}

const dayInMilliseconds = 24 * 60 * 60 * 1000

function normalizeObservationValue(value: string | undefined) {
  return (value ?? '').trim().toLowerCase().replace(/[-\s]+/g, '_')
}

function classifyMeasurement(measurement: Measurement): ObservationCategory {
  const kind = normalizeObservationValue(measurement.kind)
  const source = normalizeObservationValue(measurement.source)
  const context = measurement.data && typeof measurement.data.measurement_context === 'string'
    ? normalizeObservationValue(measurement.data.measurement_context)
    : ''
  const imported = source.includes('import') || source === 'owner_import' || kind === 'citation_import' || kind === 'legacy_import'
  if (imported) return observationCategories.unknown

  if (context === 'both') return observationCategories.lab_and_field
  if (context === 'field_or_origin') return observationCategories.field_performance
  if (context === 'lab') return observationCategories.lab

  const lab = kind === 'pagespeed' || kind === 'lighthouse' || source.includes('pagespeed') || source.includes('lighthouse')
  if (lab) return observationCategories.lab

  const fieldOrTraffic = new Set(['gsc', 'ga4', 'referral', 'referral_traffic', 'field', 'field_data', 'traffic'])
  if (fieldOrTraffic.has(kind) || fieldOrTraffic.has(source)) return observationCategories.field

  return observationCategories.unknown
}

function classifyObservationFreshness(observedAt: unknown, now = Date.now()): ObservationFreshnessInfo {
  if (typeof observedAt !== 'string' || !observedAt.trim()) return observationFreshness.unknown
  const timestamp = Date.parse(observedAt)
  if (!Number.isFinite(timestamp) || timestamp > now) return observationFreshness.unknown

  const age = now - timestamp
  if (age <= 7 * dayInMilliseconds) return observationFreshness.fresh
  if (age <= 30 * dayInMilliseconds) return observationFreshness.aging
  return observationFreshness.stale
}

function sourceKindForCollection(kind: VisibilitySourceKind) {
  return kind === 'ai_sample' ? 'ai' : kind
}

function sourceLabel(kind: string) {
  return visibilitySources.find((source) => source.kind === kind)?.label ?? titleCase(kind)
}

function measurementsForSource(measurements: Measurement[], kind: VisibilitySourceKind | string) {
  const normalizedKind = normalizeObservationValue(kind)
  const aliases = normalizedKind === 'ai' || normalizedKind === 'ai_sample' ? new Set(['ai', 'ai_sample']) : new Set([normalizedKind])
  return measurements.filter((measurement) => aliases.has(normalizeObservationValue(measurement.kind)) || aliases.has(normalizeObservationValue(measurement.source)))
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function initialDataForSeoObservationReady(connection: Connection | undefined): boolean {
  if (!connection || connection.kind !== 'dataforseo' || normalizeObservationValue(connection.status) !== 'configured') return false
  const capabilities = asRecord(connection.capabilities)
  if (capabilities?.credential_shape_verified !== true) return false
  const settings = asRecord(capabilities.settings)
  if (!settings) return false
  const pricing = asRecord(settings.pricing)
  const estimate = settings.estimated_cost_cents ?? settings.cost_cents ?? pricing?.estimated_cost_cents ?? pricing?.cost_cents
  const maximum = settings.max_cost_cents ?? pricing?.max_cost_cents
  if (typeof estimate !== 'number' || !Number.isInteger(estimate) || estimate < 0) return false
  if (maximum !== undefined && (typeof maximum !== 'number' || !Number.isInteger(maximum) || maximum <= 0 || estimate > maximum)) return false
  return (typeof maximum === 'number' ? maximum : estimate) > 0
}

function sourceState(connection: Connection | undefined, measurements: Measurement[], trackedQuestionCount?: number): VisibilitySourceState {
  const status = normalizeObservationValue(connection?.status)
  if (!connection || !status || ['needs_connection', 'not_connected', 'disconnected', 'revoked'].includes(status)) {
    return { key: 'needs_connection', label: 'Needs Connection', tone: 'amber', description: 'No verified connection is available. Collection is unavailable until this source is configured.' }
  }
  if (status === 'unsupported') {
    return { key: 'unsupported', label: 'Unsupported', tone: 'red', description: 'This source is not supported for the current connection. Choose a supported integration before collecting.' }
  }
  if (['error', 'failed', 'failure'].includes(status)) {
    return { key: 'error', label: 'Error', tone: 'red', description: 'The latest connection check failed. Review the connection and retry its test before relying on this source.' }
  }
  if (status === 'configured' && initialDataForSeoObservationReady(connection)) {
    return { key: 'needs_review', label: 'Needs Review', tone: 'amber', description: 'Credentials and pricing are ready for one bounded DataForSEO observation. That first paid collection will verify provider access and reserve the recorded maximum.' }
  }
  if (['testing', 'needs_test', 'configured'].includes(status)) {
    return { key: 'needs_review', label: 'Needs Review', tone: 'amber', description: 'This source is configured but has not passed its latest connection check. Review it before collecting.' }
  }
  if (connection?.kind === 'ai' && trackedQuestionCount === 0) {
    return { key: 'needs_review', label: 'Needs Review', tone: 'amber', description: 'Add at least one tracked question in Policies & budget before collecting an AI sample. No provider request or budget reservation will be made without a question.' }
  }

  const connectionFreshness = classifyObservationFreshness(connection.checked_at)
  const observationFreshnesses = measurements.map((measurement) => classifyObservationFreshness(measurement.observed_at).key)
  if (connectionFreshness.key === 'stale' || (observationFreshnesses.length > 0 && observationFreshnesses.every((freshness) => freshness === 'stale' || freshness === 'unknown'))) {
    return { key: 'stale', label: 'Stale', tone: 'red', description: 'The connection or its observations are older than 30 days. Collect again before treating this source as current.' }
  }
  if (measurements.length === 0) {
    return { key: 'empty', label: 'No Observations', tone: 'slate', description: 'No observations have been returned for this source. An empty result is not evidence that visibility is fully measured.' }
  }
  return { key: 'ready', label: 'Connected', tone: 'green', description: `${measurements.length} observation${measurements.length === 1 ? '' : 's'} from this source are available. This is not a complete-coverage claim.` }
}

function SourceStateNotice({
  siteId,
  kind,
  state,
  connection,
  setupPath,
  setupLabel,
  onRetry,
  retrying,
}: {
  siteId: string
  kind: string
  state: VisibilitySourceState
  connection?: Connection
  setupPath?: string
  setupLabel?: string
  onRetry: () => void
  retrying: boolean
}) {
  const label = sourceLabel(kind)
  const noticeKind = state.key === 'error' || state.key === 'unsupported' ? 'error' : state.key === 'ready' ? 'success' : state.key === 'needs_connection' || state.key === 'stale' || state.key === 'needs_review' ? 'warning' : 'info'
  const actionPath = setupPath ?? `/sites/${siteId}/settings/connections`
  const actionLabel = setupLabel ?? 'Open source connections'
  return <Notice kind={noticeKind} title={`${label}: ${state.label}`}>
    <div>{state.description}</div>
    {state.key === 'error' && connection?.error && <div className="text-small" style={{ marginTop: 5 }}>Latest detail: {truncate(connection.error, 240)}</div>}
    {(state.key === 'needs_connection' || state.key === 'error' || state.key === 'needs_review' || state.key === 'unsupported') && <div className="form-actions" style={{ marginTop: 10 }}><Link to={actionPath} className="button button-secondary button-sm">{actionLabel} <ArrowUpRight size={14} /></Link>{state.key === 'error' && <Button variant="ghost" size="sm" onClick={onRetry} disabled={retrying}><RefreshCw size={14} /> Retry status check</Button>}</div>}
  </Notice>
}

function ObservationBasisLegend() {
  return <Panel padded><div className="panel-header"><div><h2 className="panel-title">How to read observations</h2><p className="panel-subtitle">These labels describe the evidence type; none of them guarantees rankings or represents every visitor.</p></div><Eye size={18} color="#148b89" /></div><div role="list" aria-label="Observation basis legend" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 12 }}>{Object.values(observationCategories).map((category) => <div key={category.key} role="listitem" style={{ border: '1px solid #d8e2e1', borderRadius: 8, padding: 12 }}><Badge value={category.label} tone={category.tone} /><p className="text-small text-muted" style={{ margin: '8px 0 0' }}>{category.description}</p></div>)}</div></Panel>
}

function ObservationFreshnessGuide() {
  return <Panel padded><div className="panel-header"><div><h2 className="panel-title">Observation freshness</h2><p className="panel-subtitle">Freshness describes only elapsed time since the source timestamp. It does not measure accuracy, data quality, rankings, or every visitor's experience.</p></div><Eye size={18} color="#148b89" /></div><div role="list" aria-label="Observation freshness guide" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: 12 }}>{Object.values(observationFreshness).map((freshness) => <div key={freshness.key} role="listitem" style={{ border: '1px solid #d8e2e1', borderRadius: 8, padding: 12 }}><Badge value={freshness.label} tone={freshness.tone} /><p className="text-small text-muted" style={{ margin: '8px 0 0' }}>{freshness.description}</p></div>)}</div></Panel>
}

const measurementImportPlaceholder = '[{"kind":"ai_sample","source":"provider","observed_at":"2026-09-14T12:00:00Z","data":{"provider":"Example AI","model":"model-id","question":"Who can help?","locale":"en-US","answer":"…","citations":[{"url":"https://source.example/page"}]}},{"kind":"citation","source":"backlink-report","data":{"citations":[{"url":"https://source.example/referring-page","source_kind":"backlink-observation"}]}},{"kind":"technical","source":"business-listing-report","data":{"status":"observed","details":"business-listing-observation"}},{"kind":"competitor_observation","source":"competitor-research-provider","observed_at":"2026-09-17T12:00:00Z","data":{"observation_type":"competitor_observation","provider":"Example competitor provider","competitor_url":"https://competitor.example/","source_date":"2026-09-17","summary":"Provider-specific competitor observation"}}]'

export function VisibilityPage() {
  const siteId = useSiteId()
  const [tab, setTab] = useState<VisibilityTab>('observed')
  const loader = useCallback(async () => {
    const [measurements, connections, policy] = await Promise.all([operationsApi.measurements(siteId, { limit: 200 }), connectionsApi.list(siteId), policyApi.get(siteId)])
    return { measurements: measurements.items, measurementsTotal: measurements.total, connections: connections.items, policy }
  }, [siteId])
  const resource = useResource(loader, [siteId])
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [importText, setImportText] = useState('')
  const [working, setWorking] = useState(false)
  const [sourceKind, setSourceKind] = useState<VisibilitySourceKind>('gsc')

  async function collect() {
    setWorking(true); setMessage(null); setError(null)
    try {
      const job = await jobsApi.create(siteId, { kind: 'visibility', payload: { kind: sourceKind }, idempotency_key: `visibility-${sourceKind}-${Date.now()}` })
      const finished = await jobsApi.wait(siteId, job.id, { onUpdate: (next) => setMessage(`${titleCase(sourceKind)} collection is ${next.status}.`) })
      setMessage(finished.status === 'complete'
        ? `${titleCase(sourceKind)} visibility collection completed.`
        : `${titleCase(sourceKind)} collection is ${finished.status}. Review Activity for details.`)
      await resource.reload()
    }
    catch (requestError) { setError(detailMessage(requestError)) }
    finally { setWorking(false) }
  }

  async function importData() {
    setWorking(true); setMessage(null); setError(null)
    try {
      const parsed: unknown = JSON.parse(importText)
      if (!Array.isArray(parsed)) throw new Error('Imports must be a JSON array of citation or measurement records.')
      const response = await operationsApi.importMeasurements(siteId, parsed)
      setMessage(`Imported ${response.imported} validated measurement${response.imported === 1 ? '' : 's'}.`)
      setImportText('')
      setTab('observed')
      await resource.reload()
    } catch (requestError) { setError(detailMessage(requestError)) }
    finally { setWorking(false) }
  }

  return <ResourceStateView resource={resource} empty={<ErrorState message="No visibility response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => <>
      {(() => {
        const selectedConnectionKind = sourceKindForCollection(sourceKind)
        const selectedConnection = data.connections.find((item) => item.kind === selectedConnectionKind)
        const selectedMeasurements = measurementsForSource(data.measurements, sourceKind)
        const trackedQuestionCount = selectedConnectionKind === 'ai' && Array.isArray(data.policy?.settings?.tracked_questions)
          ? data.policy.settings.tracked_questions.length
          : undefined
        const selectedState = sourceState(selectedConnection, selectedMeasurements, trackedQuestionCount)
        const selectedSetupPath = selectedConnectionKind === 'ai' && trackedQuestionCount === 0 ? `/sites/${siteId}/settings/policies` : undefined
        const selectedSetupLabel = selectedSetupPath ? 'Open Policies & budget' : undefined
        const canCollect = !['needs_connection', 'error', 'unsupported'].includes(selectedState.key)
          && (selectedState.key !== 'needs_review' || initialDataForSeoObservationReady(selectedConnection))
        return <>
          <PageHeader eyebrow="Channels" title="Visibility" description="Bring in real observations from supported sources, keeping AI samples, backlink observations, and business-listing observations distinct from consumer rankings." actions={<><Button variant="secondary" onClick={() => void resource.reload()} disabled={resource.loading}><RefreshCw size={15} /> Refresh</Button><Button onClick={() => void collect()} disabled={working || !canCollect} title={canCollect ? undefined : `${sourceLabel(selectedConnectionKind)} is not ready for collection`}><Eye size={15} /> {working ? 'Submitting…' : 'Collect visibility'}</Button></>} />
          {data.measurementsTotal > data.measurements.length && <div className="mb-20"><Notice kind="warning" title="Visibility coverage is partial">Showing {data.measurements.length} of {data.measurementsTotal} observations returned by the latest response. The table and any conclusions below cover only the records returned here; an empty queue does not mean visibility is fully measured.</Notice></div>}
          <div className="filter-row" style={{ marginBottom: 18 }}><label><span className="field-label">Source to collect</span><select value={sourceKind} onChange={(event) => setSourceKind(event.target.value as VisibilitySourceKind)} disabled={working}><option value="gsc">Google Search Console</option><option value="ga4">Google Analytics 4</option><option value="pagespeed">PageSpeed</option><option value="dataforseo">DataForSEO SERP (paid)</option><option value="ai_sample">AI answer sample (paid)</option></select></label><span className="field-hint">The worker reports missing connections, unknown pricing, and provider errors instead of inventing metrics.</span></div>
          <div className="mb-20"><SourceStateNotice siteId={siteId} kind={selectedConnectionKind} state={selectedState} connection={selectedConnection} setupPath={selectedSetupPath} setupLabel={selectedSetupLabel} onRetry={() => void resource.reload()} retrying={resource.loading} /></div>
        </>
      })()}
      {message && <div className="mb-20"><Notice kind="success">{message}</Notice></div>}{error && <div className="mb-20"><Notice kind="error">{error}</Notice></div>}
      <div className="tabs" role="tablist"><button className={`tab ${tab === 'observed' ? 'active' : ''}`} onClick={() => setTab('observed')} role="tab" aria-selected={tab === 'observed'}>Observed</button><button className={`tab ${tab === 'imports' ? 'active' : ''}`} onClick={() => setTab('imports')} role="tab" aria-selected={tab === 'imports'}>Imports</button><button className={`tab ${tab === 'settings' ? 'active' : ''}`} onClick={() => setTab('settings')} role="tab" aria-selected={tab === 'settings'}>Source settings</button></div>
      {tab === 'observed' && <><ObservationBasisLegend /><ObservationFreshnessGuide /><Panel padded={false}>{data.measurements.length ? <TableShell caption="Visibility measurements"><thead><tr><th>Observation</th><th>Evidence type</th><th>Source</th><th>Observed</th><th>Freshness</th><th>Data</th></tr></thead><tbody>{data.measurements.map((measurement, index) => { const category = classifyMeasurement(measurement); const freshness = classifyObservationFreshness(measurement.observed_at); return <tr key={measurement.id ?? `${measurement.source}-${measurement.observed_at}-${index}`}><td><div className="table-primary">{titleCase(measurement.kind)}</div><div className="table-secondary">Stored measurement</div></td><td><Badge value={category.label} tone={category.tone} /><div className="table-secondary" style={{ maxWidth: 240 }}>{category.description}</div></td><td><Badge value={measurement.source} /></td><td className="text-muted">{formatDateTime(measurement.observed_at)}</td><td><Badge value={freshness.label} tone={freshness.tone} /><div className="table-secondary" style={{ maxWidth: 240 }}>{freshness.description}</div></td><td><div className="json-preview" style={{ maxWidth: 420, maxHeight: 74 }}>{truncate(JSON.stringify(measurement.data), 240)}</div></td></tr> })}</tbody></TableShell> : <EmptyState icon={<Database size={20} />} title="No observations yet" description="Collect from a real source or import validated citation records. Empty metrics are intentionally not filled with estimates." action={<Button variant="secondary" size="sm" onClick={() => setTab('imports')}><FileInput size={14} /> Import data</Button>} />}</Panel></>}
      {tab === 'imports' && <div className="grid-2"><Panel padded><div className="panel-header"><div><h2 className="panel-title">Import validated observations</h2><p className="panel-subtitle">Paste the JSON array you received from a supported research or citation workflow.</p></div><UploadCloud size={18} color="#148b89" /></div><Field label="JSON array" hint="The API preserves source, provider, model, question, locale, answer, citations, and observation time when supplied. Backlink observations and business-listing observations are recommendation inputs only: they are not ranking proof and do not trigger automatic changes. Competitor observations are provider-specific observations: include their source and date; they are not a guarantee of this site's ranking and do not trigger automatic changes. Never paste API keys, passwords, tokens, cookies, or other credentials here; the API rejects them before storage."><textarea value={importText} onChange={(event) => setImportText(event.target.value)} placeholder={measurementImportPlaceholder} style={{ minHeight: 270, fontFamily: 'ui-monospace, monospace', fontSize: '.76rem' }} /></Field><div className="form-actions"><Button onClick={() => void importData()} disabled={working || !importText.trim()}><UploadCloud size={15} /> {working ? 'Validating…' : 'Validate & import'}</Button></div></Panel><Panel padded><div className="panel-header"><div><h2 className="panel-title">Import guidance</h2><p className="panel-subtitle">Keep the distinction visible in your reporting.</p></div><Search size={18} color="#148b89" /></div><div className="stack-sm"><div className="notice notice-info"><Search size={15} /><div><strong>AI samples</strong> are observations of a provider response, not a promise of consumer search rank.</div></div><div className="notice notice-warning"><Download size={15} /><div><strong>Provenance matters.</strong> Unknown pricing, unsupported citations, and fabricated metrics should be rejected by the API.</div></div><div className="notice notice-warning"><Download size={15} /><div><strong>Keep credentials out.</strong> Never paste API keys, passwords, tokens, cookies, or other login details into an import. The API rejects credentials before anything is stored.</div></div><div className="notice notice-info"><Database size={15} /><div><strong>Backlink observations and business-listing observations</strong> are evidence for recommendations only. They are not proof of rankings and do not make automatic changes.</div></div><div className="notice notice-info"><Database size={15} /><div><strong>Competitor observations</strong> are provider-specific observations with a source and date. They are not a guarantee of this site's ranking and do not make automatic changes.</div></div><div className="notice notice-info"><Database size={15} /><div><strong>Separate reports</strong> can be imported as AI samples, citation reports, referral traffic, or technical eligibility observations.</div></div></div></Panel></div>}
      {tab === 'settings' && <Panel padded><div className="panel-header"><div><h2 className="panel-title">Visibility sources</h2><p className="panel-subtitle">Connect a source before asking the worker to collect from it. Secrets stay masked and live in Connections.</p></div><Link to={`/sites/${siteId}/settings/connections`} className="button button-secondary button-sm">Open connections <ArrowUpRight size={14} /></Link></div><div>{visibilitySources.map(({ kind, label }) => { const connection = data.connections.find((item) => item.kind === kind); const trackedQuestionCount = kind === 'ai' && Array.isArray(data.policy?.settings?.tracked_questions) ? data.policy.settings.tracked_questions.length : undefined; const state = sourceState(connection, measurementsForSource(data.measurements, kind), trackedQuestionCount); return <div className="source-card" key={kind}><div><div className="source-name">{label}</div><div className="source-detail">{connection?.checked_at ? `Last checked ${formatDateTime(connection.checked_at)}` : 'No check recorded'} · {state.description}</div></div><div style={{ display: 'flex', alignItems: 'center', gap: 10 }}><Badge value={state.label} tone={state.tone} /><Link to={`/sites/${siteId}/settings/connections`} aria-label={`Configure ${kind}`} className="icon-button"><Settings2 size={16} /></Link></div></div>})}</div></Panel>}
    </>}
  </ResourceStateView>
}
