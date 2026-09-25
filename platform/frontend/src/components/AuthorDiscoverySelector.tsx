import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { authorsApi, detailMessage } from '../lib/api'
import { formatDateTime } from '../lib/format'
import type { AuthorDiscovery } from '../types'
import { Button, Field, Notice } from './ui'

interface DiscoveryState {
  siteId: string
  response: AuthorDiscovery | null
  loading: boolean
  error: string | null
}

export interface AuthorDiscoveryState extends DiscoveryState {
  authors: AuthorDiscovery['items']
  isVerified: (authorId: string | null | undefined) => boolean
  refresh: () => void
}

function normalizeDiscovery(value: AuthorDiscovery): AuthorDiscovery {
  if (!value || !Array.isArray(value.items) || typeof value.complete !== 'boolean'
    || !(value.checked_at === null || typeof value.checked_at === 'string')
    || !(value.authenticated_user_id === null || typeof value.authenticated_user_id === 'string')
    || !Array.isArray(value.blockers)) {
    throw new Error('The author discovery response was incomplete. Refresh before selecting an author.')
  }
  const items = value.items.map((item) => {
    if (!item || typeof item.id !== 'string' || !item.id.trim() || typeof item.name !== 'string' || !item.name.trim()) {
      throw new Error('The author discovery response contained an invalid author record. No authors from this check are verified.')
    }
    return item
  })
  if (value.blockers.some((item) => typeof item !== 'string')) {
    throw new Error('The author discovery response contained invalid blockers. No authors from this check are verified.')
  }
  return {
    ...value,
    items,
    blockers: value.blockers,
    warnings: Array.isArray(value.warnings) ? value.warnings.filter((item): item is string => typeof item === 'string') : [],
  }
}

export function useAuthorDiscovery(siteId: string): AuthorDiscoveryState {
  const [attempt, setAttempt] = useState(0)
  const [state, setState] = useState<DiscoveryState>({ siteId: '', response: null, loading: true, error: null })

  useEffect(() => {
    let active = true
    setState({ siteId, response: null, loading: true, error: null })
    void authorsApi.discover(siteId).then((response) => {
      const normalized = normalizeDiscovery(response)
      if (active) setState({ siteId, response: normalized, loading: false, error: null })
    }).catch((requestError: unknown) => {
      if (active) setState({ siteId, response: null, loading: false, error: detailMessage(requestError) })
    })
    return () => { active = false }
  }, [siteId, attempt])

  const refresh = useCallback(() => {
    setState({ siteId, response: null, loading: true, error: null })
    setAttempt((current) => current + 1)
  }, [siteId])
  const current = state.siteId === siteId
    ? state
    : { siteId, response: null, loading: true, error: null }
  const authors = current.response?.items ?? []
  const isVerified = (authorId: string | null | undefined) => !authorId?.trim()
    || Boolean(current.response?.complete && current.response.blockers.length === 0 && current.response.authenticated_user_id && authors.some((author) => author.id === authorId))

  return { ...current, authors, isVerified, refresh }
}

export function AuthorDiscoverySelector({ discovery, value, onChange, disabled = false }: {
  discovery: AuthorDiscoveryState
  value: string
  onChange: (value: string) => void
  disabled?: boolean
}) {
  const selectedAuthor = discovery.authors.find((author) => author.id === value)
  const selectedIsVerified = discovery.isVerified(value)
  const canChooseAuthors = Boolean(discovery.response?.complete && discovery.response.blockers.length === 0 && discovery.response.authenticated_user_id)

  return <div className="author-discovery">
    <Field label="Publishing author" hint="Choose an author returned by the latest complete check of the authenticated WordPress connection.">
      <select aria-label="Publishing author" value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
        <option value="">No author selected</option>
        {value && !selectedAuthor && <option value={value}>Saved author {value} (unverified)</option>}
        {canChooseAuthors && discovery.authors.map((author) => <option key={author.id} value={author.id}>{author.name} ({author.id})</option>)}
      </select>
    </Field>
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8, marginTop: 8 }}>
      <Button type="button" variant="secondary" size="sm" onClick={discovery.refresh} disabled={discovery.loading || disabled}>
        <RefreshCw size={14} /> {discovery.loading ? 'Checking authors…' : 'Refresh authors'}
      </Button>
      {discovery.response && <span className="text-small text-muted">Last check: {formatDateTime(discovery.response.checked_at, 'Date not returned')}</span>}
    </div>
    {discovery.loading && <p role="status" className="text-small text-muted" style={{ margin: '9px 0 0' }}>Loading authors from the authenticated WordPress connection…</p>}
    {!discovery.loading && discovery.error && <Notice kind="error" title="Author discovery failed">{discovery.error} Refresh authors before choosing an author. A saved ID is not verified by this failed check.</Notice>}
    {!discovery.loading && !discovery.error && discovery.response?.complete && discovery.authors.length === 0 && <Notice kind="warning" title="No authors returned">The latest author check completed but returned no authors. You can still save a blank review draft.</Notice>}
    {!discovery.loading && !discovery.error && discovery.response && !discovery.response.complete && <Notice kind="warning" title="Author discovery is incomplete">Authors from an incomplete check are not verified. Resolve the blockers and refresh before saving with an author.</Notice>}
    {!discovery.loading && discovery.response && discovery.response.blockers.length > 0 && <Notice kind="warning" title="Author check has blockers">No author from this response can be treated as verified until every blocker is cleared.</Notice>}
    {!discovery.loading && discovery.response && discovery.response.blockers.length > 0 && <ul className="compact-list" aria-label="Author discovery blockers">{discovery.response.blockers.map((blocker, index) => <li key={`${index}-${blocker}`}>{blocker}</li>)}</ul>}
    {!discovery.loading && discovery.response && discovery.response.warnings?.map((warning, index) => <Notice kind="info" key={`${index}-${warning}`} title="Author listing warning">{warning === 'connection_can_only_assign_self' ? 'This WordPress connection can assign only its authenticated account as an author.' : warning}</Notice>)}
    {value && !selectedIsVerified && <Notice kind="warning" title="Selected author is unverified">Saved author {value} was not returned by the latest complete author check. Clear it to save a blank-author draft, or refresh discovery before saving.</Notice>}
  </div>
}
