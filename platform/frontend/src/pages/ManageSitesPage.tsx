import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Archive, ArrowRight, Plus, RotateCcw, Search } from 'lucide-react'
import { Badge, Button, EmptyState, ErrorState, Field, LoadingState, Notice, PageHeader, Panel } from '../components/ui'
import { useAuth, useSites } from '../context/AppContext'
import { detailMessage, sitesApi } from '../lib/api'
import type { Site } from '../types'

export function ManageSitesPage() {
  const { role } = useAuth()
  const { refresh } = useSites()
  const [sites, setSites] = useState<Site[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<'active' | 'archived'>('active')
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  async function load() {
    setLoading(true)
    setError(null)
    try { setSites((await sitesApi.list(true)).items) }
    catch (e) { setError(detailMessage(e)) }
    finally { setLoading(false) }
  }

  useEffect(() => { void load() }, [])

  async function changeArchive(site: Site, archive: boolean) {
    setBusyId(site.id)
    setError(null)
    setMessage(null)
    try {
      const updated = await (archive ? sitesApi.archive(site.id) : sitesApi.restore(site.id))
      setSites((current) => current.map((item) => item.id === site.id ? updated : item))
      setConfirmId(null)
      setMessage(archive ? `${site.name} archived. Its history is saved; you can restore it from Archived sites.`
        : `${site.name} restored. Automation is paused. Review its connections and policies before enabling it.`)
      await refresh()
      if (!archive) setFilter('active')
    } catch (e) { setError(detailMessage(e)) }
    finally { setBusyId(null) }
  }

  const activeCount = sites.filter((site) => !site.archived_at).length
  const archivedCount = sites.length - activeCount
  const matching = sites.filter((site) => Boolean(site.archived_at) === (filter === 'archived')
    && `${site.name} ${site.origin}`.toLowerCase().includes(query.trim().toLowerCase()))

  return <>
    <PageHeader eyebrow="Your workspace" title="Manage sites"
      description="Add a website, open its dashboard, or archive a site you no longer manage."
      actions={role === 'owner' && <Link className="button button-primary button-md" to="/sites/new"><Plus size={16} />Add site</Link>} />
    {message && <Notice kind="success">{message}</Notice>}
    {error && <ErrorState message={error} onRetry={() => void load()} />}
    <div className="manage-site-tools">
      <Field label="Search sites"><div className="manage-site-search"><Search size={16} /><input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Site name or website address" /></div></Field>
      <div className="manage-site-filters" role="group" aria-label="Site status">
        <Button variant={filter === 'active' ? 'primary' : 'secondary'} aria-pressed={filter === 'active'} onClick={() => { setFilter('active'); setConfirmId(null) }}>Active sites ({activeCount})</Button>
        <Button variant={filter === 'archived' ? 'primary' : 'secondary'} aria-pressed={filter === 'archived'} onClick={() => { setFilter('archived'); setConfirmId(null) }}>Archived sites ({archivedCount})</Button>
      </div>
    </div>
    {loading ? <LoadingState label="Loading your sites" /> : !error && !matching.length ?
      <EmptyState title={query ? 'No matching sites' : filter === 'archived' ? 'No archived sites' : 'Add your first active site'}
        description={query ? 'Try another name or website address.' : filter === 'archived' ? 'Sites you archive will appear here so you can restore them.' : archivedCount ? 'Add another website or restore a site from Archived sites.' : 'Start by adding your website. Each site gets its own dashboard, settings, and reports.'}
        action={!query && filter === 'active' && role === 'owner' ? <Link className="button button-primary button-md" to="/sites/new">Add site</Link> : undefined} /> :
      <div className="managed-sites-grid">{matching.map((site) => <Panel className="managed-site-card" key={site.id}>
        <div className="managed-site-heading"><h2>{site.name}</h2><Badge value={site.archived_at ? 'Archived' : site.paused ? 'Automation paused' : 'Site enabled'} tone="slate" /></div>
        <p className="managed-site-origin">{site.origin}</p>
        <p className="text-small text-muted">{site.timezone} · {site.language.toUpperCase()}</p>
        <div className="managed-site-actions">
          {!site.archived_at && <Link className="button button-secondary button-md" to={`/sites/${site.id}/overview`}>Open dashboard<ArrowRight size={15} /></Link>}
          {role === 'owner' && (site.archived_at ? <Button variant="secondary" disabled={!!busyId} onClick={() => void changeArchive(site, false)}><RotateCcw size={15} />{busyId === site.id ? 'Restoring…' : 'Restore site'}</Button>
            : <Button variant="ghost" disabled={!!busyId} onClick={() => setConfirmId(site.id)}><Archive size={15} />Archive site</Button>)}
        </div>
        {confirmId === site.id && <div className="site-archive-confirm" role="group" aria-label={`Archive ${site.name}`}>
          <p>Archive {site.name}? This stops monitoring and cancels queued work. Your website stays online and its ForgeSEO history is saved. You can restore it later.</p>
          <div className="managed-site-actions"><Button variant="danger" disabled={!!busyId} onClick={() => void changeArchive(site, true)}>{busyId === site.id ? 'Archiving…' : 'Confirm archive'}</Button><Button variant="secondary" disabled={!!busyId} onClick={() => setConfirmId(null)}>Keep site</Button></div>
        </div>}
      </Panel>)}</div>}
  </>
}
