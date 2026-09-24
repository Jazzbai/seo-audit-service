import { useState } from 'react'
import { articlesApi, detailMessage } from '../lib/api'
import type { Article } from '../types'
import { Button, Field, Notice, Panel } from './ui'

function urlOf(value: unknown): string | null {
  const raw = typeof value === 'string' ? value : value && typeof value === 'object' && 'url' in value ? value.url : null
  if (typeof raw !== 'string') return null
  try {
    const url = new URL(raw)
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null
    url.hash = ''
    return url.href
  } catch { return null }
}

export function SourceReviewPanel({ siteId, article, disabled, onReviewed }: {
  siteId: string; article: Article; disabled: boolean; onReviewed: () => Promise<void>
}) {
  const [selected, setSelected] = useState('')
  const [notes, setNotes] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [working, setWorking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const generation = article.brief?.generation as Record<string, unknown> | undefined
  const flagged = Array.isArray(generation?.unverified_sources) ? generation.unverified_sources : []
  const urls = [...new Set([...flagged, ...article.sources].map(urlOf).filter((url): url is string => Boolean(url)))]
  const accepted = article.source_review_state?.accepted_urls ?? []
  const held = disabled || working || ['scheduled', 'publishing', 'verifying', 'published'].includes(article.status)

  async function review() {
    if (held || !article.updated_at || !selected || !confirmed || notes.trim().length < 30) return
    setWorking(true); setError(null); setMessage(null)
    try {
      await articlesApi.reviewSource(siteId, article.id, { url: selected, notes: notes.trim(), expected_updated_at: article.updated_at, confirms_claim_support: confirmed })
      await onReviewed()
      setNotes(''); setConfirmed(false)
      setMessage('Source review recorded for this saved revision. Publishing remains governed by the site policy and pauses.')
    } catch (err) { setError(detailMessage(err)) }
    finally { setWorking(false) }
  }

  return <Panel padded>
    <h2 className="panel-title">Source review</h2>
    <p className="panel-subtitle">Read the source and the saved article before recording which claims or link purpose it supports. Fetch success alone is not fact-checking.</p>
    <div className="stack-sm">
      <p className="text-small text-muted">Original model flags stay in history. Reviews expire after seven days and become invalid when the title, body, source list or generation changes.</p>
      {disabled && <Notice kind="warning">Save any article changes before reviewing its sources.</Notice>}
      <Field label="Source to review"><select value={selected} onChange={event => { setSelected(event.target.value); setConfirmed(false); setNotes(''); setMessage(null) }} disabled={held}>
        <option value="">Choose a source</option>
        {urls.map(url => <option key={url} value={url}>{url}</option>)}
      </select></Field>
      {selected && <a href={selected} target="_blank" rel="noreferrer">Open selected source</a>}
      {selected && accepted.includes(selected) && <Notice kind="success">Review is current for this saved revision.</Notice>}
      <Field label="Source review notes" hint="Explain what you verified, and any limits. Do not enter passwords or confidential information."><textarea value={notes} onChange={event => setNotes(event.target.value)} minLength={30} maxLength={2000} disabled={held} /></Field>
      <label className="checkbox-field"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={held} /><span>I reviewed this source against the saved article and confirm the stated support.</span></label>
      <Button type="button" variant="secondary" onClick={() => void review()} disabled={held || !selected || !confirmed || notes.trim().length < 30 || !article.updated_at}>{working ? 'Verifying source…' : 'Record source review'}</Button>
      {message && <Notice kind="success">{message}</Notice>}
      {error && <Notice kind="error">{error}</Notice>}
      {accepted.length > 0 && <p className="text-small text-muted">{accepted.length} source review(s) currently valid. This does not approve publication.</p>}
    </div>
  </Panel>
}
