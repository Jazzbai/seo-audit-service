import { Link } from 'react-router-dom'
import { Panel } from './ui'
import { formatCurrencyCents } from '../lib/format'

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
}

export function ProviderUsagePanel({ brief, siteId }: { brief: unknown; siteId: string }) {
  const generation = object(object(brief).generation)
  const usage = object(generation.usage)
  const rows = [
    ['Input tokens', count(usage.input_tokens) ?? count(usage.prompt_tokens)],
    ['Output tokens', count(usage.output_tokens) ?? count(usage.completion_tokens)],
    ['Total tokens', count(usage.total_tokens)],
  ] as const
  const estimate = count(generation.estimated_cost_cents)
  const maximum = count(generation.max_cost_cents)
  return <section aria-label="Provider usage"><Panel padded>
    <h2 className="panel-title">Provider usage</h2>
    <p className="text-small text-muted">Recorded generation usage, not an invoice or editorial approval.</p>
    {rows.map(([label, value]) => <div className="metric-row" key={label}><span>{label}</span><strong>{value === null ? 'Not recorded' : value.toLocaleString('en-US')}</strong></div>)}
    <div className="metric-row"><span>Configured estimate</span><strong>{estimate === null ? 'Unknown' : formatCurrencyCents(estimate)}</strong></div>
    <div className="metric-row"><span>Request reservation ceiling</span><strong>{maximum === null ? 'Unknown' : formatCurrencyCents(maximum)}</strong></div>
    <p className="text-small text-muted">The estimate and ceiling are not actual charges. Unknown billing does not mean free usage.</p>
    <Link to={`/sites/${siteId}/settings/policies`}>Review held reservations and actual charges</Link>
  </Panel></section>
}
