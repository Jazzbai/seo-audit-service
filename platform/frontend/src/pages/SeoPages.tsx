import { useCallback, useMemo, useState } from 'react'
import { ExternalLink, FileSearch, Filter, RefreshCw, Search, Sparkles, WandSparkles } from 'lucide-react'
import { Button, Badge, EmptyState, ErrorState, Notice, PageHeader, Panel, TableShell } from '../components/ui'
import { detailMessage, jobsApi, pagesApi } from '../lib/api'
import { formatDateTime, truncate, titleCase } from '../lib/format'
import type { Candidate, Finding, PageRecord } from '../types'
import { ResourceStateView, useResource, useSiteId } from './shared'
import { useAuth } from '../context/AppContext'

const PAGE_DAILY_RECONCILIATION_MS = 24 * 60 * 60 * 1000
const RESULT_PAGE_SIZE = 50

type PageFreshness = {
  label: 'Within daily cadence' | 'Stale' | 'Freshness unknown'
  tone: 'teal' | 'amber'
  description: string
}

/**
 * Classify only the age of the stored inventory observation. Missing, invalid,
 * and future timestamps stay unknown; they are never presented as current.
 * This is not evidence that a page is optimized or that its data is complete.
 */
function pageFreshness(lastSeenAt?: string | null, now = Date.now()): PageFreshness {
  if (typeof lastSeenAt !== 'string' || !lastSeenAt.trim()) {
    return {
      label: 'Freshness unknown',
      tone: 'amber',
      description: 'No valid observation timestamp is available; freshness is unknown, not current.',
    }
  }

  const timestamp = Date.parse(lastSeenAt)
  if (!Number.isFinite(timestamp) || timestamp > now) {
    return {
      label: 'Freshness unknown',
      tone: 'amber',
      description: 'The observation timestamp is invalid or in the future; freshness is unknown, not current.',
    }
  }

  if (timestamp < now - PAGE_DAILY_RECONCILIATION_MS) {
    return {
      label: 'Stale',
      tone: 'amber',
      description: 'Observed more than 24 hours ago; refresh inventory before relying on this record.',
    }
  }

  return {
    label: 'Within daily cadence',
    tone: 'teal',
    description: 'Observed within the 24-hour reconciliation cadence. This reflects timestamp age only, not data quality or optimization.',
  }
}

export function IssuesPage() {
  const siteId = useSiteId()
  const { role } = useAuth()
  const [query, setQuery] = useState('')
  const [findingOffset, setFindingOffset] = useState(0)
  const [candidateOffset, setCandidateOffset] = useState(0)
  const [severity, setSeverity] = useState('all')
  const [actionError, setActionError] = useState<string | null>(null)
  const [actionMessage, setActionMessage] = useState<string | null>(null)
  const [working, setWorking] = useState<string | null>(null)
  const loader = useCallback(async () => {
    const [findings, candidates] = await Promise.all([
      pagesApi.findings(siteId, { limit: RESULT_PAGE_SIZE, offset: findingOffset }),
      pagesApi.candidates(siteId, { limit: RESULT_PAGE_SIZE, offset: candidateOffset }),
    ])
    return {
      findings: findings.items,
      candidates: candidates.items,
      findingTotal: findings.total,
      candidateTotal: candidates.total,
    }
  }, [siteId, findingOffset, candidateOffset])
  const resource = useResource(loader, [siteId, findingOffset, candidateOffset])

  const filteredFindings = useMemo(() => resource.data?.findings.filter((finding) => (severity === 'all' || finding.severity === severity) && (!query || `${finding.title} ${finding.code}`.toLowerCase().includes(query.toLowerCase()))) ?? [], [resource.data, severity, query])

  async function decide(candidate: Candidate, decision: 'approve' | 'reject') {
    setActionError(null)
    setActionMessage(null)
    setWorking(candidate.id)
    try {
      await pagesApi.decide(siteId, candidate.id, decision)
      setActionMessage(`Candidate ${decision === 'approve' ? 'approved' : 'rejected'}.`)
      await resource.reload()
    } catch (error) {
      setActionError(detailMessage(error))
    } finally {
      setWorking(null)
    }
  }

  async function execute(candidate: Candidate) {
    setActionError(null)
    setActionMessage(null)
    setWorking(candidate.id)
    try {
      const job = await pagesApi.execute(siteId, candidate.id)
      const finished = await jobsApi.wait(siteId, job.id, { onUpdate: (next) => setActionMessage(`Candidate execution is ${next.status}. The server will recheck policy and source state before writing.`) })
      setActionMessage(finished.status === 'complete' ? 'Candidate execution completed and its verification result is recorded.' : `Candidate execution is ${finished.status}. Review the publication or incident record.`)
      await resource.reload()
    } catch (error) {
      setActionError(detailMessage(error))
    } finally {
      setWorking(null)
    }
  }

  return <ResourceStateView resource={resource} empty={<ErrorState message="No issue response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => <>
      <PageHeader eyebrow="Growth & SEO" title="Issues" description="Review findings and candidate changes before anything touches a connected site." actions={<Button variant="secondary" onClick={() => void resource.reload()} disabled={resource.loading}><RefreshCw size={15} /> Refresh</Button>} />
      {actionMessage && <div className="mb-20"><Notice kind="success">{actionMessage}</Notice></div>}
      {actionError && <div className="mb-20"><Notice kind="error">{actionError}</Notice></div>}
      <div className="split-panel">
        <Panel padded={false}>
          <div style={{ padding: '22px 22px 0' }}><div className="panel-header"><div><h2 className="panel-title">Findings & history</h2><p className="panel-subtitle">Filters apply to this result page. Empty results do not mean the site is fully optimized.</p></div><span className="kicker">{data.findingTotal} total</span></div><div className="filter-row"><label className="search-wrap"><Search size={15} /><span className="sr-only">Search issues</span><input placeholder="Search this page by title or code" value={query} onChange={event => setQuery(event.target.value)} /></label><label><span className="sr-only">Filter severity</span><select value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="all">All severities</option><option value="critical">Critical</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select></label><Filter size={15} color="#8b9798" /></div><ResultPagination ariaLabel="Finding results" offset={findingOffset} total={data.findingTotal} onChange={setFindingOffset} /></div>
          {filteredFindings.length ? <TableShell caption="Open findings"><thead><tr><th>Finding</th><th>Severity</th><th>Status</th><th>Last seen</th></tr></thead><tbody>{filteredFindings.map((finding) => <tr key={finding.id}><td><div className="issue-title"><span className={`severity-bar ${finding.severity}`} /><div><div className="table-primary">{finding.title}</div><div className="table-secondary">{finding.code} · {truncate(typeof finding.details?.summary === 'string' ? finding.details.summary : finding.key, 92)}</div></div></div></td><td><Badge value={finding.severity} /></td><td><Badge value={finding.status} /></td><td className="text-muted">{formatDateTime(finding.last_seen_at)}</td></tr>)}</tbody></TableShell> : <EmptyState icon={<FileSearch size={20} />} title={data.findingTotal ? 'No matching findings on this page' : severity === 'all' ? 'No findings recorded' : `No ${severity} findings`} description={data.findingTotal ? 'The API has findings, but none match this page and filter. Change the filter or pagination to inspect the remaining records.' : 'Run an audit or change the filter when the API has more signals to show.'} />}
        </Panel>
        <Panel padded>
          <div className="panel-header"><div><h2 className="panel-title">Candidate history</h2><p className="panel-subtitle">Each suggestion is independent. It is either awaiting a person’s decision or explicitly authorized by the site policy.</p></div><Badge value={`${data.candidateTotal} total`} /></div>
          <ResultPagination ariaLabel="Candidate results" offset={candidateOffset} total={data.candidateTotal} onChange={setCandidateOffset} />
          {data.candidates.length ? data.candidates.map((candidate) => <CandidateCard key={candidate.id} candidate={candidate} canEdit={role === 'owner' || role === 'editor'} working={working === candidate.id} onDecide={decide} onExecute={execute} />) : data.candidateTotal > 0 ? <EmptyState icon={<Sparkles size={20} />} title="No candidates on this page" description="The API reports candidate history, but this result page has no rows. Use the candidate pagination above to inspect the remaining independent records." /> : <EmptyState icon={<Sparkles size={20} />} title="No candidates waiting" description="No candidate changes are currently waiting for a decision. This only means the queue is empty; it does not mean the site is fully optimized. Check audit coverage and findings for unresolved or unmeasured work." />}
        </Panel>
      </div>
    </>}
  </ResourceStateView>
}

function CandidateCard({ candidate, canEdit, working, onDecide, onExecute }: { candidate: Candidate; canEdit: boolean; working: boolean; onDecide: (candidate: Candidate, decision: 'approve' | 'reject') => void; onExecute: (candidate: Candidate) => void }) {
  const detail = candidate.details ?? {}
  const reviewOnlyReasons = Array.isArray(detail.review_only_reasons) ? detail.review_only_reasons.filter((reason): reason is string => typeof reason === 'string' && Boolean(reason.trim())) : []
  const pageUrl = candidate.page?.url ?? (typeof detail.url === 'string' ? detail.url : '')
  return <div className="candidate-card"><div className="candidate-top"><span className="candidate-field">{titleCase(candidate.field)}</span><Badge value={candidate.status} /></div><div className="candidate-diff"><div className="diff-box"><span className="diff-label">Current</span>{candidate.before_value || 'No explicit value'}</div><div className="diff-box after"><span className="diff-label">Suggested</span>{candidate.after_value}</div></div>{pageUrl && <a href={pageUrl} target="_blank" rel="noreferrer" className="table-secondary" style={{ display: 'inline-flex', gap: 5, marginTop: 10 }}>Open page <ExternalLink size={12} /></a>}{reviewOnlyReasons.length > 0 && <div className="text-small" style={{ marginTop: 10, color: '#8a5a00' }}><strong>Review only:</strong> this change needs a connector that can write SEO metadata for this resource.</div>}<div className="candidate-actions">{reviewOnlyReasons.length > 0 ? canEdit && candidate.status === 'pending' ? <Button variant="ghost" size="sm" onClick={() => onDecide(candidate, 'reject')} disabled={working}>Dismiss</Button> : <span className="text-small">Not executable with the current connection.</span> : !canEdit || !['pending', 'approved'].includes(candidate.status) ? <span className="text-small">{canEdit ? 'Retained for review; not eligible for retry.' : 'Read-only for your role.'}</span> : candidate.status === 'approved' ? <Button size="sm" onClick={() => onExecute(candidate)} disabled={working}><WandSparkles size={14} /> {working ? 'Submitting…' : 'Execute approved change'}</Button> : <><Button variant="ghost" size="sm" onClick={() => onDecide(candidate, 'reject')} disabled={working}>Reject</Button><Button size="sm" onClick={() => onDecide(candidate, 'approve')} disabled={working}>Approve</Button></>}</div></div>
}

export function PagesPage() {
  const siteId = useSiteId()
  const { role } = useAuth()
  const [offset, setOffset] = useState(0)
  const [query, setQuery] = useState('')
  const [enrollment, setEnrollment] = useState('all')
  const [working, setWorking] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const loader = useCallback(() => pagesApi.list(siteId, { limit: 50, offset }), [siteId, offset])
  const resource = useResource(loader, [siteId, offset])

  const filtered = resource.data?.items.filter((page) => {
    const matchesQuery = !query || `${page.title ?? ''} ${page.url} ${page.resource_key}`.toLowerCase().includes(query.toLowerCase())
    const matchesEnrollment = enrollment === 'all' || (enrollment === 'enrolled' ? page.enrolled : !page.enrolled)
    return matchesQuery && matchesEnrollment
  }) ?? []

  async function toggleEnrollment(page: PageRecord) {
    setWorking(page.id)
    setMessage(null)
    setError(null)
    try {
      await pagesApi.enroll(siteId, page.id, !page.enrolled)
      setMessage(`${page.enrolled ? 'Removed from' : 'Enrolled in'} managed coverage: ${page.title || page.url}`)
      await resource.reload()
    } catch (requestError) {
      setError(detailMessage(requestError))
    } finally {
      setWorking(null)
    }
  }

  async function refreshInventory() {
    setWorking('inventory')
    setMessage(null)
    setError(null)
    try {
      const job = await jobsApi.create(siteId, { kind: 'inventory', payload: {}, idempotency_key: `inventory-${Date.now()}` })
      const finished = await jobsApi.wait(siteId, job.id, { onUpdate: (next) => setMessage(`Inventory is ${next.status}. The connector is reconciling stored records.`) })
      setMessage(finished.status === 'complete' ? 'Inventory reconciled. Refresh this list to see the latest stored records and missing-resource markers.' : `Inventory is ${finished.status}. Review Activity or Incidents for details.`)
      await resource.reload()
    } catch (requestError) {
      setError(detailMessage(requestError))
    } finally {
      setWorking(null)
    }
  }

  return <ResourceStateView resource={resource} empty={<ErrorState message="No page inventory response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => <>
      <PageHeader eyebrow="Growth & SEO" title="Pages" description="Choose which discovered pages can receive managed editorial coverage." actions={<><Button variant="secondary" onClick={() => void resource.reload()} disabled={resource.loading}><RefreshCw size={15} /> Refresh</Button><Button onClick={() => void refreshInventory()} disabled={working === 'inventory' || role === 'viewer'}><RefreshCw size={15} /> {working === 'inventory' ? 'Starting…' : 'Refresh inventory'}</Button></>} />
      {message && <div className="mb-20"><Notice kind="success">{message}</Notice></div>}{error && <div className="mb-20"><Notice kind="error">{error}</Notice></div>}
      <ResultPagination ariaLabel="Page results" offset={offset} total={data.total} onChange={setOffset} />
      <Panel padded={false}><div style={{ padding: '22px 22px 0' }}><div className="panel-header"><div><h2 className="panel-title">Page inventory</h2><p className="panel-subtitle">{data.total} stored records, not a complete-coverage claim. Filters apply to this result page. Enrollment is explicit.</p></div><Badge value={`${filtered.length} shown`} /></div><div className="filter-row"><label className="search-wrap"><Search size={15} /><span className="sr-only">Search pages</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search URL or title" /></label><label><span className="sr-only">Filter enrollment</span><select value={enrollment} onChange={(event) => setEnrollment(event.target.value)}><option value="all">All pages</option><option value="enrolled">Enrolled</option><option value="not_enrolled">Not enrolled</option></select></label></div><div style={{ marginBottom: 18 }}><Notice kind="info" title="Observation freshness only">Freshness uses the daily reconciliation cadence: records observed within 24 hours are within cadence, older records are stale, and missing, invalid, or future timestamps are unknown. These labels describe timestamp age only; they do not prove data quality, complete coverage, or optimization.</Notice></div></div>{filtered.length ? <TableShell caption="Page inventory"><thead><tr><th>Page</th><th>Type</th><th>Last seen and freshness</th><th>Coverage</th><th /></tr></thead><tbody>{filtered.map((page) => { const freshness = pageFreshness(page.last_seen_at); return <tr key={page.id}><td><div className="table-primary">{page.title || 'Untitled page'}</div><a className="table-secondary table-url" href={page.url} target="_blank" rel="noreferrer">{page.url}</a>{Boolean(page.signals?.evidence) && <div><a className="table-secondary" href={`/api/v1/sites/${encodeURIComponent(siteId)}/pages/${encodeURIComponent(page.id)}/evidence`} download>Download captured HTML</a></div>}</td><td><span className="text-muted">{titleCase(page.resource_type ?? 'page')}</span></td><td><div className="text-muted">{formatDateTime(page.last_seen_at)}</div><Badge value={freshness.label} tone={freshness.tone} /><div className="table-secondary">{freshness.description}</div></td><td><Badge value={page.enrolled ? 'enrolled' : 'not enrolled'} /></td><td><div className="table-actions"><Button size="sm" variant={page.enrolled ? 'ghost' : 'secondary'} onClick={() => void toggleEnrollment(page)} disabled={working === page.id || role !== 'owner'}>{working === page.id ? 'Saving…' : page.enrolled ? 'Remove' : 'Enroll'}</Button></div></td></tr> })}</tbody></TableShell> : <EmptyState icon={<GlobeIcon />} title="No pages match" description="There are no page records for this filter yet. Refresh inventory when the connector is ready." action={<Button variant="secondary" size="sm" onClick={() => void refreshInventory()}><RefreshCw size={14} /> Refresh inventory</Button>} />}</Panel>
    </>}
  </ResourceStateView>
}

function GlobeIcon() { return <span style={{ fontSize: '1.1rem' }}>◎</span> }

function ResultPagination({ariaLabel,offset,total,onChange}:{ariaLabel:string;offset:number;total:number;onChange:(offset:number)=>void}) {
  const hasPage = total > 0 && offset < total
  const first = hasPage ? offset + 1 : 0
  const last = hasPage ? Math.min(offset + RESULT_PAGE_SIZE, total) : 0
  return <nav aria-label={ariaLabel} className="filter-row"><Button size="sm" variant="secondary" disabled={offset===0} onClick={()=>onChange(Math.max(0,offset-RESULT_PAGE_SIZE))}>Previous results</Button><span className="text-small">{total ? hasPage ? `${first}–${last} of ${total}` : `0 on this page · ${total} total` : '0 records'}</span><Button size="sm" variant="secondary" disabled={offset+RESULT_PAGE_SIZE>=total} onClick={()=>onChange(offset+RESULT_PAGE_SIZE)}>Next results</Button></nav>
}
