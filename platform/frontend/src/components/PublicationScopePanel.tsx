import { useEffect, useState } from 'react'
import type { Article, PolicySettings } from '../types'
import { articlesApi, detailMessage } from '../lib/api'
import { Button, Notice, Panel } from './ui'

export function PublicationScopePanel({ siteId, settings, disabled, onChange }: {
  siteId: string; settings: PolicySettings; disabled: boolean; onChange: (ids: string[] | null) => void
}) {
  const [articles, setArticles] = useState<Article[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let cancelled = false
    setLoaded(false); setError(null)
    articlesApi.list(siteId, { limit: 200 }).then(result => {
      if (!cancelled) { setArticles(result.items); setLoaded(true) }
    }).catch(err => { if (!cancelled) setError(detailMessage(err)) })
    return () => { cancelled = true }
  }, [siteId, retry])
  const scope = settings.publication_article_ids
  const restricted = Array.isArray(scope)
  const missing = (scope ?? []).filter(id => !articles.some(article => article.id === id))
  return <Panel padded>
    <h2 className="panel-title">Article publishing scope</h2>
    <p className="panel-subtitle">Limit a pilot to named drafts. Save with “Save policy controls” below. This never removes pauses, protected paths, editorial checks or weekly limits.</p>
    <label className="checkbox-field"><input type="checkbox" checked={restricted} disabled={disabled || !loaded} onChange={event => onChange(event.target.checked ? [] : null)} /><span>Restrict publishing to selected articles</span></label>
    {restricted ? <div className="stack-sm">
      <p className="text-small text-muted">Unselected and newly generated drafts cannot publish. Automatic research-and-publish cycles are disabled for this restricted policy.</p>
      {scope.length === 0 && <Notice kind="warning">No articles selected: publication is blocked for every article.</Notice>}
      {articles.map(article => <label className="checkbox-field" key={article.id}><input type="checkbox" checked={scope.includes(article.id)} disabled={disabled || !loaded} onChange={event => onChange(event.target.checked ? [...scope, article.id] : scope.filter(id => id !== article.id))} /><span>{article.title} ({article.status})</span></label>)}
      {missing.length > 0 && <Notice kind="warning">Some saved selections are not in this page of results. They are preserved; review the complete inventory before changing scope.</Notice>}
    </div> : <Notice kind="warning">No article-ID restriction. Other publication safeguards still apply.</Notice>}
    {!loaded && !error && <p>Loading article choices…</p>}
    {error && <Notice kind="error">Article choices could not load: {error} <Button variant="ghost" onClick={() => setRetry(value => value + 1)}>Retry article choices</Button></Notice>}
  </Panel>
}
