import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { Eye, EyeOff, History, KeyRound, LockKeyhole, PlugZap, Save, ShieldCheck, Trash2, UserPlus, Users, Zap } from 'lucide-react'
import { Badge, Button, EmptyState, ErrorState, Field, Notice, PageHeader, Panel } from '../components/ui'
import { budgetsApi, connectionsApi, detailMessage, jobsApi, operationsApi, policyApi, settingsApi, sitesApi, teamApi } from '../lib/api'
import { formatCurrencyCents, formatDateTime, formatNumber, titleCase } from '../lib/format'
import { useAuth } from '../context/AppContext'
import type { Connection, Policy, PolicySettings, Site } from '../types'
import { ResourceStateView, useResource, useSiteId, stringList } from './shared'
import { PublicationScopePanel } from '../components/PublicationScopePanel'
import { AuthorDiscoverySelector, useAuthorDiscovery } from '../components/AuthorDiscoverySelector'
import { MicrosoftGraphConnectionCard } from '../components/MicrosoftGraphConnectionCard'

export function SettingsLayout() {
  const siteId = useSiteId()
  return <><PageHeader eyebrow="Workspace" title="Settings" description="Connections, people, and the rules that keep site changes reviewable." /><div className="settings-layout"><nav className="settings-nav" aria-label="Settings navigation"><NavLink to={`/sites/${siteId}/settings/business`}>Business</NavLink><NavLink to={`/sites/${siteId}/settings/connections`}>Connections</NavLink><NavLink to={`/sites/${siteId}/settings/team`}>Team</NavLink><NavLink to={`/sites/${siteId}/settings/policies`}>Policies & budget</NavLink></nav><div><Outlet /></div></div></>
}

interface ConnectionField {
  key: string
  label: string
  placeholder: string
  type?: 'text' | 'checkbox'
  hint?: string
  secret?: boolean
  format?: 'csv'
  maxItems?: number
  itemMaxLength?: number
  maxLength?: number
  target: 'credentials' | 'settings'
}

interface ConnectionDefinition { kind: string; name: string; description: string; fields: ConnectionField[] }

const CONNECTIONS: ConnectionDefinition[] = [
  { kind: 'wordpress', name: 'WordPress', description: 'Inventory, content reads, and protected editorial writes.', fields: [{ key: 'username', label: 'Username', placeholder: 'wp-editor', target: 'credentials' }, { key: 'application_password', label: 'Application password', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }, { key: 'webhook_secret', label: 'Webhook secret', placeholder: 'Optional; leave blank to keep it unset', hint: 'Optional, site-scoped, and stored encrypted. Only needed when the optional ForgeSEO connector sends signed change notifications. Periodic polling continues without it.', secret: true, target: 'credentials' }] },
  { kind: 'woocommerce', name: 'WooCommerce', description: 'Product editorial fields only; commercial fields remain protected.', fields: [{ key: 'consumer_key', label: 'Consumer key', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }, { key: 'consumer_secret', label: 'Consumer secret', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }] },
  { kind: 'gsc', name: 'Google Search Console', description: 'Search performance and verified property observations.', fields: [{ key: 'site_url', label: 'Property', placeholder: 'sc-domain:example.com', target: 'settings' }, { key: 'client_id', label: 'Client ID', placeholder: 'OAuth client ID', target: 'credentials' }, { key: 'client_secret', label: 'Client secret', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }, { key: 'refresh_token', label: 'Refresh token', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }] },
  { kind: 'ga4', name: 'Google Analytics 4', description: 'Analytics observations through an authorized property.', fields: [{ key: 'property_id', label: 'Property ID', placeholder: '123456789', maxLength: 64, target: 'settings' }, { key: 'conversion_event_names', label: 'Conversion event names', placeholder: 'generate_lead, purchase', hint: 'Optional. Comma-separated GA4 event names to report as business conversions; up to 12 names. This does not create or change events in Google Analytics.', format: 'csv', maxItems: 12, itemMaxLength: 40, maxLength: 512, target: 'settings' }, { key: 'dimensions', label: 'Reporting dimensions', placeholder: 'date, eventName', hint: 'Optional. Comma-separated GA4 dimensions, up to 8. Include eventName when you need to compare conversion events; leave blank for the default date view.', format: 'csv', maxItems: 8, itemMaxLength: 64, maxLength: 512, target: 'settings' }, { key: 'metrics', label: 'Reporting metrics', placeholder: 'sessions, conversions', hint: 'Optional. Comma-separated GA4 metrics, up to 10. Include conversions for conversion-aware reporting; leave blank for the default sessions and users view.', format: 'csv', maxItems: 10, itemMaxLength: 64, maxLength: 512, target: 'settings' }, { key: 'client_id', label: 'Client ID', placeholder: 'OAuth client ID', target: 'credentials' }, { key: 'client_secret', label: 'Client secret', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }, { key: 'refresh_token', label: 'Refresh token', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }] },
  { kind: 'dataforseo', name: 'DataForSEO', description: 'Optional keyword, SERP, and policy-driven competitor observations with provider pricing.', fields: [{ key: 'login', label: 'Login', placeholder: 'Provider login', target: 'credentials' }, { key: 'password', label: 'Password', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }, { key: 'location_code', label: 'Location code', placeholder: '2840', hint: 'Required for competitor observations. Use the DataForSEO location code for the site’s target market.', target: 'settings' }] },
  { kind: 'ai', name: 'AI sample provider', description: 'Provider-scoped citation samples, kept distinct from consumer rankings.', fields: [{ key: 'provider', label: 'Provider', placeholder: 'OpenAI', target: 'settings' }, { key: 'endpoint', label: 'Endpoint', placeholder: 'https://api.openai.com/v1/responses', target: 'settings' }, { key: 'request_format', label: 'Request format', placeholder: 'openai_responses_web_search', hint: 'For OpenAI web-grounded samples use openai_responses_web_search. The API requires a completed answer with explicit citations.', target: 'settings' }, { key: 'locale', label: 'Locale', placeholder: 'en-US (optional)', target: 'settings' }, { key: 'search_context_size', label: 'Search context size', placeholder: 'low, medium, or high', hint: 'Low is the conservative default. Higher context can increase provider usage.', target: 'settings' }, { key: 'api_key', label: 'API key', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }] },
  { kind: 'pagespeed', name: 'PageSpeed', description: 'Performance observations for the connected public origin.', fields: [{ key: 'api_key', label: 'API key', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }, { key: 'url', label: 'Sample URL', placeholder: 'Optional; defaults to the site origin', target: 'settings' }, { key: 'strategy', label: 'Strategy', placeholder: 'mobile or desktop', target: 'settings' }] },
  { kind: 'smtp', name: 'SMTP', description: 'Optional delivery for reports and operational notifications.', fields: [{ key: 'host', label: 'SMTP host', placeholder: 'smtp.example.com', target: 'settings' }, { key: 'sender', label: 'From email', placeholder: 'reports@example.com', target: 'settings' }, { key: 'username', label: 'Username', placeholder: 'SMTP username', target: 'credentials' }, { key: 'password', label: 'Password', placeholder: 'Leave blank to keep stored secret', secret: true, target: 'credentials' }, { key: 'digest_enabled', label: 'Send weekly email digests', placeholder: '', hint: 'Turn this off to keep in-app reports while suppressing optional SMTP delivery.', target: 'settings', type: 'checkbox' }] },
  { kind: 'microsoft_graph', name: 'Microsoft 365 Graph', description: 'Owner-managed Graph authentication with an explicitly scoped one-message test.', fields: [] },
]

for (const definition of CONNECTIONS) {
  if (['ai','dataforseo'].includes(definition.kind)) definition.fields.push(
    {key:'estimated_cost_cents',label:'Estimated request cost (cents)',placeholder:'Enter verified provider pricing',hint: definition.kind === 'ai' ? 'For OpenAI Responses web search, enter the estimate for one tracked question; the weekly reservation multiplies it by the question count.' : undefined,target:'settings'},
    {key:'max_cost_cents',label:'Maximum request cost (cents)',placeholder:'Reserved before each paid request',hint: definition.kind === 'ai' ? 'For OpenAI Responses web search, this is the maximum for one tracked question; the weekly reservation multiplies it by the question count.' : undefined,target:'settings'},
  )
  if (definition.kind === 'ai') definition.fields.push({key:'model',label:'Model',placeholder:'Provider model ID',hint:'Choose a model enabled for your OpenAI project. ForgeSEO will not guess a model or spend until one is configured.',target:'settings'})
  if (definition.kind === 'smtp') definition.fields.push({key:'recipients',label:'Recipients',placeholder:'Comma-separated email addresses',target:'settings'})
}

type CapabilityState = 'automatic' | 'read-only' | 'needs review' | 'needs connection' | 'unsupported' | 'degraded'

interface CapabilitySummaryItem {
  label: string
  state: CapabilityState
  description: string
}

const CAPABILITY_TONES: Record<CapabilityState, 'teal' | 'amber' | 'red' | 'slate' | 'blue' | 'green'> = {
  automatic: 'green',
  'read-only': 'slate',
  'needs review': 'amber',
  'needs connection': 'amber',
  unsupported: 'red',
  degraded: 'red',
}

const DEGRADED_CONNECTION_STATUSES = ['error', 'failed', 'failure', 'degraded']

function capabilityRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function capabilityFlag(value: unknown, key: string): boolean {
  return capabilityRecord(value)[key] === true
}

function capabilityList(value: unknown, key: string): unknown[] {
  const result = capabilityRecord(value)[key]
  return Array.isArray(result) ? result : []
}

function connectionReadiness(connection?: Connection): { state: Exclude<CapabilityState, 'read-only'>; description: string } {
  const status = connection?.status?.toLowerCase() ?? ''
  const test = capabilityRecord(connection?.capabilities?.last_connection_test)
  const testStatus = typeof test.status === 'string' ? test.status.toLowerCase() : ''
  if (!connection || !status || ['needs_connection', 'revoked', 'disconnected'].includes(status)) {
    return { state: 'needs connection', description: 'Connect this source before ForgeSEO can verify access.' }
  }
  if (status === 'unsupported') {
    return { state: 'unsupported', description: 'This source is not supported for this site.' }
  }
  if (DEGRADED_CONNECTION_STATUSES.includes(status) || DEGRADED_CONNECTION_STATUSES.includes(testStatus)) {
    return { state: 'degraded', description: 'The last provider check failed. Review the connection and run Test again before relying on it.' }
  }
  const verified = status === 'connected' && (
    testStatus === 'verified'
    || (Boolean(connection.checked_at) && Object.keys(connection.capabilities ?? {}).length > 0)
  )
  if (verified) {
    return { state: 'automatic', description: 'Access was verified. Workflows remain subject to policy and editorial checks.' }
  }
  return { state: 'needs review', description: 'Credentials or authorization are present, but access is not verified yet. Run Test before relying on this source.' }
}

function ConnectionReadinessSummary({ definition, connection }: { definition: ConnectionDefinition; connection?: Connection }) {
  const readiness = connectionReadiness(connection)
  return <div role="status" aria-label={`${definition.name} connection readiness: ${titleCase(readiness.state)}`} style={{ marginBottom: 16, padding: '10px 12px', border: '1px solid var(--line)', borderRadius: 9, background: 'var(--surface-muted)' }}>
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10 }}><strong className="text-small">Connection readiness</strong><Badge value={readiness.state} tone={CAPABILITY_TONES[readiness.state]} /></div>
    <div className="text-small text-muted" style={{ marginTop: 5 }}>{readiness.description}</div>
    <div className="text-small text-muted" style={{ marginTop: 5 }}>Last checked: {formatDateTime(connection?.checked_at, 'Not yet checked')}</div>
  </div>
}

function wordpressNotificationCapability(connection?: Connection): CapabilitySummaryItem {
  const status = connection?.status?.toLowerCase()
  if (!connection || !status || ['needs_connection', 'revoked', 'disconnected'].includes(status)) {
    return { label: 'Targeted change notifications', state: 'needs connection', description: 'Connect WordPress to enable optional signed change notifications. Periodic polling continues without them.' }
  }
  if (status === 'unsupported') {
    return { label: 'Targeted change notifications', state: 'unsupported', description: 'This connection cannot provide optional signed change notifications. Periodic polling continues, so monitoring is not disabled.' }
  }
  if (['error', 'failed', 'failure', 'degraded'].includes(status ?? '')) {
    return { label: 'Targeted change notifications', state: 'degraded', description: 'The WordPress connection check failed, so targeted notifications are degraded. Periodic polling continues meanwhile.' }
  }
  if (status !== 'connected') {
    return { label: 'Targeted change notifications', state: 'needs review', description: 'Run a successful WordPress capability test before notifications can be verified. Periodic polling continues meanwhile.' }
  }
  if (!connection.capabilities || Object.keys(connection.capabilities).length === 0) {
    return { label: 'Targeted change notifications', state: 'needs review', description: 'The connection is present, but notification capability has not been verified yet. Periodic polling continues meanwhile.' }
  }

  const capabilities = capabilityRecord(connection.capabilities)
  const plugins = capabilityRecord(capabilities.plugins)
  const forgeseo = capabilityRecord(plugins.forgeseo ?? capabilities.forgeseo_plugin)
  if (capabilityFlag(forgeseo, 'webhooks')) {
    return { label: 'Targeted change notifications', state: 'automatic', description: 'The verified ForgeSEO connector can send signed change notifications for targeted checks. Periodic polling remains active as a fallback.' }
  }
  if (capabilityFlag(forgeseo, 'detected')) {
    return { label: 'Targeted change notifications', state: 'needs review', description: 'The ForgeSEO connector is present, but signed notifications are not advertised as available. Review its configuration; periodic polling continues.' }
  }
  return { label: 'Targeted change notifications', state: 'unsupported', description: 'The optional ForgeSEO connector is not installed or does not expose signed notifications. Periodic polling continues, so monitoring is not disabled.' }
}

function wordpressResourceCoverage(connection?: Connection): CapabilitySummaryItem[] {
  const capabilities = capabilityRecord(connection?.capabilities)
  const raw = capabilities.resource_types
  if (!Array.isArray(raw)) {
    return [{
      label: 'Editorial resource coverage',
      state: 'needs review',
      description: 'The latest WordPress check did not report content-type coverage. Page reading alone does not prove that the whole site inventory is complete.',
    }]
  }

  const malformed = raw.some((value) => {
    const resource = capabilityRecord(value)
    return typeof resource.key !== 'string' || !resource.key.trim()
  })
  const resources = raw
    .map((value) => capabilityRecord(value))
    .filter((resource) => typeof resource.key === 'string' && resource.key.trim())

  if (!resources.length) {
    return [{
      label: 'Editorial resource coverage',
      state: 'needs review',
      description: 'No WordPress content types were returned. Treat the current page result as partial until a fresh capability check reports safe coverage.',
    }]
  }

  const items = resources.map((resource) => {
    const key = String(resource.key).trim()
    const label = typeof resource.label === 'string' && resource.label.trim()
      ? resource.label.trim()
      : typeof resource.name === 'string' && resource.name.trim()
        ? resource.name.trim()
        : key
    const editorialWrite = capabilityRecord(resource.editorial_write)
    const inventoryable = resource.inventoryable === true
    const automatic = inventoryable && editorialWrite.supported === true && editorialWrite.automatic === true
    return {
      label: `Editorial type: ${label}`,
      state: automatic ? 'automatic' : inventoryable ? 'read-only' : 'needs review',
      description: automatic
        ? `${label} is safely inventoryable and has a verified native editorial contract.`
        : inventoryable
          ? `${label} is inventoryable for audit and planning only. Editorial writes require a specific connector contract.`
          : `${label} was reported by WordPress, but safe inventory coverage is not verified yet.`,
    } satisfies CapabilitySummaryItem
  })

  if (malformed) {
    items.push({
      label: 'Additional editorial resource coverage',
      state: 'needs review',
      description: 'Some resource descriptors were malformed or incomplete. Do not treat the listed types as the complete site inventory until the next check is clean.',
    })
  }
  return items
}

function connectionGate(connection?: Connection): CapabilitySummaryItem | null {
  const status = connection?.status?.toLowerCase()
  if (!connection || !status || ['needs_connection', 'revoked', 'disconnected'].includes(status)) {
    return { label: 'Connection', state: 'needs connection', description: 'Connect this source before ForgeSEO can inspect its capabilities.' }
  }
  if (status === 'unsupported') {
    return { label: 'Connection', state: 'unsupported', description: 'This connection type is not supported for this site.' }
  }
  if (['error', 'failed', 'failure', 'degraded'].includes(status)) {
    return { label: 'Connection', state: 'degraded', description: 'The latest connection check failed. Review the connection before relying on these capabilities.' }
  }
  if (status !== 'connected') {
    return { label: 'Connection', state: 'needs review', description: 'Review the connection and run a test before using these capabilities.' }
  }
  if (!connection.capabilities || Object.keys(connection.capabilities).length === 0) {
    return { label: 'Capability check', state: 'needs review', description: 'The connection is present, but its capability check has not returned results yet.' }
  }
  return null
}

function wordpressCapabilitySummary(connection?: Connection): CapabilitySummaryItem[] {
  const gate = connectionGate(connection)
  if (gate) return [gate, wordpressNotificationCapability(connection)]
  const capabilities = capabilityRecord(connection?.capabilities)
  const native = capabilityRecord(capabilities.native)
  const editorial = capabilityRecord(capabilities.editorial)
  const seo = capabilityRecord(capabilities.seo)
  const builder = capabilityRecord(editorial.builder_constraints)
  const items: CapabilitySummaryItem[] = []

  if (capabilityFlag(native, 'read') || capabilityFlag(editorial, 'read')) {
    items.push({ label: 'Inventory and page reading', state: 'automatic', description: 'Read WordPress pages, posts, authors, and available content signals.' })
  } else {
    items.push({ label: 'Inventory and page reading', state: 'unsupported', description: 'The authenticated WordPress REST inventory is not available.' })
  }

  items.push(...wordpressResourceCoverage(connection))

  if (capabilityFlag(editorial, 'write') && capabilityFlag(native, 'update')) {
    items.push({ label: 'Supported editorial fields', state: 'automatic', description: 'Update supported WordPress editorial fields when site policy allows it.' })
  } else if (capabilityFlag(editorial, 'read') || capabilityFlag(native, 'read')) {
    items.push({ label: 'Supported editorial fields', state: 'read-only', description: 'Read existing content; the current connection cannot update these fields.' })
  } else {
    items.push({ label: 'Supported editorial fields', state: 'unsupported', description: 'No supported WordPress editorial write surface was verified.' })
  }

  if (capabilityFlag(native, 'create') && capabilityFlag(native, 'publish')) {
    items.push({ label: 'Article publishing', state: 'automatic', description: 'Create and publish supported articles after policy and editorial checks.' })
  } else if (capabilityFlag(native, 'create')) {
    items.push({ label: 'Article publishing', state: 'needs review', description: 'Article creation is available, but publish permission needs review.' })
  } else if (capabilityFlag(native, 'read')) {
    items.push({ label: 'Article publishing', state: 'read-only', description: 'Existing articles can be read; automatic article creation is unavailable.' })
  } else {
    items.push({ label: 'Article publishing', state: 'unsupported', description: 'Article creation is not available through this connection.' })
  }

  if (capabilityFlag(builder, 'detected') || capabilityList(editorial, 'conditionally_writable_fields').length > 0) {
    items.push({ label: 'Builder-managed body edits', state: 'needs review', description: 'Review each target before changing content managed by a page builder.' })
  }

  if (capabilityFlag(seo, 'write') && capabilityList(seo, 'writable_fields').length > 0) {
    items.push({ label: 'SEO metadata', state: 'automatic', description: 'Write the SEO fields explicitly exposed by the verified connection.' })
  } else if (capabilityFlag(seo, 'read')) {
    items.push({ label: 'SEO metadata', state: 'read-only', description: 'Read SEO metadata; no approved SEO write surface is available.' })
  } else {
    items.push({ label: 'SEO metadata', state: 'unsupported', description: 'No supported SEO metadata write surface was verified.' })
  }
  items.push(wordpressNotificationCapability(connection))
  return items
}

function woocommerceCapabilitySummary(connection?: Connection): CapabilitySummaryItem[] {
  const gate = connectionGate(connection)
  if (gate) return [gate]
  const capabilities = capabilityRecord(connection?.capabilities)
  const items: CapabilitySummaryItem[] = []
  if (!capabilityFlag(capabilities, 'woocommerce_api')) {
    return [{ label: 'Catalog connection', state: 'unsupported', description: 'The WooCommerce REST catalog API is not available.' }]
  }

  for (const [key, label] of [['products', 'Product catalog'], ['categories', 'Product categories']] as const) {
    const resource = capabilityRecord(capabilities[key])
    if (capabilityFlag(resource, 'read') && capabilityFlag(resource, 'update')) {
      items.push({ label, state: 'automatic', description: `Read and update supported ${label.toLowerCase()} editorial fields.` })
    } else if (capabilityFlag(resource, 'read')) {
      items.push({ label, state: 'read-only', description: `Read ${label.toLowerCase()}; editorial updates are not available.` })
    } else {
      items.push({ label, state: 'unsupported', description: `The current connection does not expose ${label.toLowerCase()} access.` })
    }
  }

  const seo = capabilityRecord(capabilities.seo)
  const seoResources = capabilityList(seo, 'resource_types')
  const productSeoDetected = seoResources.length === 0 || seoResources.includes('product')
  const categorySeoDetected = seoResources.includes('category')
  if (productSeoDetected && capabilityFlag(seo, 'write') && capabilityList(seo, 'writable_fields').length > 0) {
    items.push({ label: 'Product SEO metadata', state: 'automatic', description: 'Write only verified product titles and descriptions through the ForgeSEO connector.' })
  } else if (productSeoDetected && capabilityFlag(seo, 'read')) {
    items.push({ label: 'Product SEO metadata', state: 'read-only', description: 'Read product SEO metadata; no approved product SEO write surface is available.' })
  } else {
    items.push({ label: 'Product SEO metadata', state: 'unsupported', description: 'Product SEO metadata needs the optional verified connector; category SEO remains review-only.' })
  }
  if (categorySeoDetected && capabilityFlag(seo, 'write') && capabilityList(seo, 'writable_fields').length > 0) {
    items.push({ label: 'Category SEO metadata', state: 'automatic', description: 'Write only verified product-category titles and descriptions through the ForgeSEO connector.' })
  } else if (categorySeoDetected && capabilityFlag(seo, 'read')) {
    items.push({ label: 'Category SEO metadata', state: 'read-only', description: 'Read product-category SEO metadata; no approved category SEO write surface is available.' })
  } else {
    items.push({ label: 'Category SEO metadata', state: 'unsupported', description: 'Category SEO metadata needs a verified taxonomy route and archive-rendering contract.' })
  }

  if (capabilityFlag(capabilities, 'authenticated') !== true) {
    items.push({ label: 'Permission verification', state: 'needs review', description: 'The API is present, but authenticated permission details need another check.' })
  }
  if (capabilityList(capabilities, 'protected_commerce_fields').length > 0) {
    items.push({ label: 'Commercial data', state: 'unsupported', description: 'Prices, stock, SKUs, orders, payments, and customer data stay protected.' })
  }
  return items
}

function googleCapabilitySummary(definition: ConnectionDefinition, connection?: Connection) {
  const status = connection?.status?.toLowerCase()
  const test = capabilityRecord(connection?.capabilities?.last_connection_test)
  const testStatus = typeof test.status === 'string' ? test.status.toLowerCase() : ''
  let state: CapabilitySummaryItem['state'] = 'needs connection'
  let description = 'Save the Google OAuth credentials and complete authorization before ForgeSEO can read this source.'
  if (['needs_connection', 'revoked', 'disconnected', ''].includes(status ?? '')) {
    state = 'needs connection'
  } else if (status === 'connected' && testStatus === 'verified') {
    state = 'automatic'
    description = `Read-only ${definition.name} access was verified. ForgeSEO can collect observations, but it cannot change provider data.`
  } else if (DEGRADED_CONNECTION_STATUSES.includes(testStatus) || DEGRADED_CONNECTION_STATUSES.includes(status ?? '')) {
    state = 'degraded'
    description = 'The last provider check failed. Review the property and authorization, then run the connection test again.'
  } else {
    state = 'needs review'
    description = 'OAuth material is saved, but provider access has not been verified yet. Run the connection test before collecting measurements.'
  }
  return <section aria-labelledby={`${definition.kind}-connection-summary`} style={{ marginBottom: 16 }}>
    <div style={{ marginBottom: 8 }}><strong id={`${definition.kind}-connection-summary`} className="text-small">Connection check</strong><div className="text-small text-muted">Provider access is separate from saving credentials.</div></div>
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: 6, padding: '9px 0', borderTop: '1px solid var(--line)', borderBottom: '1px solid var(--line)' }}>
      <div><div className="text-small" style={{ fontWeight: 700 }}>{definition.name} access</div><div className="text-small text-muted">{description}</div></div>
      <Badge value={state} tone={CAPABILITY_TONES[state]} />
    </div>
  </section>
}

function ConnectionCapabilitySummary({ definition, connection }: { definition: ConnectionDefinition; connection?: Connection }) {
  const siteId = useSiteId()
  const { role } = useAuth()
  if (definition.kind === 'gsc' || definition.kind === 'ga4') return <><GoogleOAuthControl definition={definition} siteId={siteId} canEdit={role === 'owner'} />{googleCapabilitySummary(definition, connection)}</>
  if (definition.kind !== 'wordpress' && definition.kind !== 'woocommerce') return null
  const items = definition.kind === 'wordpress' ? wordpressCapabilitySummary(connection) : woocommerceCapabilitySummary(connection)
  return <section aria-labelledby={`${definition.kind}-capability-summary`} style={{ marginBottom: 16 }}>
    <div style={{ marginBottom: 8 }}><strong id={`${definition.kind}-capability-summary`} className="text-small">Capability summary</strong><div className="text-small text-muted">Based on the latest connection status and capability check.</div></div>
    <ul aria-label={`${definition.name} capability details`} style={{ listStyle: 'none', margin: 0, padding: 0, borderTop: '1px solid var(--line)' }}>
      {items.map((item) => <li key={`${item.label}-${item.state}`} style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: 6, padding: '9px 0', borderBottom: '1px solid var(--line)' }}><div><div className="text-small" style={{ fontWeight: 700 }}>{item.label}</div><div className="text-small text-muted">{item.description}</div></div><Badge value={item.state} tone={CAPABILITY_TONES[item.state]} /></li>)}
    </ul>
  </section>
}

export function ConnectionsPage() {
  const siteId = useSiteId()
  const { role } = useAuth()
  const canEdit = role === 'owner'
  const loader = useCallback(() => connectionsApi.list(siteId), [siteId])
  const resource = useResource(loader, [siteId])
  const location = useLocation()
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    const params = new URLSearchParams(location.search)
    const result = params.get('oauth')
    const kind = params.get('kind')
    const provider = kind === 'gsc' ? 'Google Search Console' : kind === 'ga4' ? 'Google Analytics 4' : null
    if (!provider || !['connected', 'error'].includes(result ?? '')) return
    if (result === 'connected') {
      setError(null)
      setMessage(`${provider} is connected. Run a connection test before collecting measurements.`)
      return
    }
    const reason = params.get('reason')
    const explanation = reason === 'authorization_denied'
      ? 'Google authorization was not completed.'
      : reason === 'token_exchange_failed'
        ? 'Google returned an unusable token response. Check the client settings and try again.'
        : 'Google authorization could not be completed.'
    setMessage(null)
    setError(`${provider}: ${explanation}`)
  }, [location.search])
  return <ResourceStateView resource={resource} empty={<ErrorState message="No connections response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => <><div className="panel-header" style={{ marginBottom: 18 }}><div><h2 className="panel-title">Connected sources</h2><p className="panel-subtitle">Secrets are encrypted by the API. Blank secret fields keep an existing secret unchanged.</p></div><Badge value={`${data.total} configured`} /></div>{!canEdit && <div className="mb-20"><Notice kind="warning" title="Read-only for your role">Only the owner can change connection credentials or submit connection tests. The API remains the authority.</Notice></div>}{message && <div className="mb-20"><Notice kind="success">{message}</Notice></div>}{error && <div className="mb-20"><Notice kind="error">{error}</Notice></div>}<div className="connection-grid">{CONNECTIONS.map((definition) => {
      const connection = data.items.find((item) => item.kind === definition.kind)
      const key = `${siteId}:${definition.kind}`
      return definition.kind === 'microsoft_graph'
        ? <MicrosoftGraphConnectionCard key={key} connection={connection} siteId={siteId} canEdit={canEdit} onChanged={async (nextMessage) => { setMessage(nextMessage); await resource.reload() }} />
        : <ConnectionCard key={key} definition={definition} connection={connection} siteId={siteId} canEdit={canEdit} onChanged={async (nextMessage) => { setMessage(nextMessage); await resource.reload() }} onError={setError} />
    })}</div></>}
  </ResourceStateView>
 }

function GoogleOAuthControl({ definition, siteId, canEdit }: { definition: ConnectionDefinition; siteId: string; canEdit: boolean }) {
  if (definition.kind !== 'gsc' && definition.kind !== 'ga4') return null
  const provider = definition.kind === 'gsc' ? 'Search Console' : 'Analytics 4'
  return <div className="oauth-connect" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 14, padding: 12, marginBottom: 16, border: '1px solid var(--line)', borderRadius: 10, background: 'var(--surface-muted)' }}>
    <div><strong className="text-small">Connect with Google</strong><div className="text-small text-muted">Save the OAuth client ID and secret first, then authorize read-only {provider} access. Tokens stay encrypted on this site.</div></div>
    {canEdit ? <a className="button button-secondary button-sm" href={connectionsApi.oauthStartUrl(siteId, definition.kind)}><KeyRound size={14} /> Connect with Google</a> : <span className="text-small text-muted">Owner access required</span>}
  </div>
}

function ConnectionCard({ definition, connection, siteId, canEdit, onChanged, onError }: { definition: ConnectionDefinition; connection?: Connection; siteId: string; canEdit: boolean; onChanged: (message: string) => Promise<void>; onError: (message: string) => void }) {
  const [values, setValues] = useState<Record<string, string>>({})
  const [showSecrets, setShowSecrets] = useState<Record<string, boolean>>({})
  const [working, setWorking] = useState(false)
  const [testResult, setTestResult] = useState<string | null>(null)
  useEffect(() => {
    const safe = { ...((connection?.capabilities?.settings ?? {}) as Record<string, unknown>), ...(connection?.safe_fields ?? {}), ...(connection?.settings ?? {}) }
    const next: Record<string, string> = {}
    for (const field of definition.fields) {
      if (field.secret) next[field.key] = ''
      else if (field.type === 'checkbox') next[field.key] = safe[field.key] === false ? 'false' : 'true'
      else if (field.format === 'csv' && Array.isArray(safe[field.key])) next[field.key] = (safe[field.key] as unknown[]).map((item) => String(item).trim()).filter(Boolean).join(', ')
      else next[field.key] = String(safe[field.key] ?? '')
    }
    setValues(next)
  }, [connection, definition.fields])
  function update(key: string, value: string) {
    const field = definition.fields.find((item) => item.key === key)
    const bounded = field?.maxLength ? value.slice(0, field.maxLength) : value
    setValues((current) => ({ ...current, [key]: bounded }))
  }
  function boundedCsv(field: ConnectionField, value: string): string[] {
    return value.split(',').map((item) => item.trim()).filter(Boolean).slice(0, field.maxItems ?? 10).map((item) => item.slice(0, field.itemMaxLength ?? 64))
  }
  async function save(event: FormEvent) {
    event.preventDefault(); if (!canEdit) return; setWorking(true); setTestResult(null)
    try {
      const credentials: Record<string, string> = {}; const settings: Record<string, unknown> = {}
      for (const field of definition.fields) {
        if (field.type === 'checkbox') { settings[field.key] = values[field.key] !== 'false'; continue }
        const value = values[field.key]?.trim(); if (!value) continue
        if (field.target === 'credentials') credentials[field.key] = value
        else if (field.format === 'csv') { const items = boundedCsv(field, value); if (items.length) settings[field.key] = items }
        else settings[field.key] = ['estimated_cost_cents','max_cost_cents','location_code'].includes(field.key) ? Number(value) : field.key === 'recipients' ? value.split(',').map(item => item.trim()).filter(Boolean) : value
      }
      await connectionsApi.save(siteId, definition.kind, { credentials, settings })
      await onChanged(`${definition.name} settings saved.`)
    } catch (requestError) { onError(detailMessage(requestError)) }
    finally { setWorking(false) }
  }
  async function test() {
    if (!canEdit) return; setWorking(true); setTestResult(null); onError('')
    try {
      const job = await connectionsApi.test(siteId, definition.kind)
      const finished = await jobsApi.wait(siteId, job.id, { onUpdate: (next) => setTestResult(`${definition.name} test is ${next.status}.`) })
      setTestResult(`${definition.name} test is ${finished.status}.`)
      await onChanged(`${definition.name} test finished with status ${finished.status}.`)
    }
    catch (requestError) { onError(detailMessage(requestError)) }
    finally { setWorking(false) }
  }
  async function revoke() {
    if (!canEdit) return
    if (!window.confirm(`Revoke the ${definition.name} connection?`)) return
    setWorking(true); setTestResult(null)
    try { await connectionsApi.revoke(siteId, definition.kind); await onChanged(`${definition.name} connection revoked.`) }
    catch (requestError) { onError(detailMessage(requestError)) }
    finally { setWorking(false) }
  }
  if (!canEdit) return <div className="connection-card"><div className="connection-card-header"><div><div className="connection-card-name">{definition.name}</div><div className="connection-card-description">{definition.description}</div></div></div><ConnectionReadinessSummary definition={definition} connection={connection} /><ConnectionCapabilitySummary definition={definition} connection={connection} /><Notice kind="warning">Owner access is required to edit this connection or submit a test.</Notice></div>
  return <form className="connection-card" onSubmit={(event) => void save(event)}><div className="connection-card-header"><div><div className="connection-card-name">{definition.name}</div><div className="connection-card-description">{definition.description}</div></div></div><ConnectionReadinessSummary definition={definition} connection={connection} /><ConnectionCapabilitySummary definition={definition} connection={connection} /><div className="connection-fields">{definition.fields.map((field) => <Field key={field.key} label={field.label} hint={field.hint ?? (field.secret && connection && connection.status !== 'needs_connection' ? 'Stored securely · leave blank to keep it.' : undefined)}>{field.secret ? <div className="secret-field"><input aria-label={field.label} type={showSecrets[field.key] ? 'text' : 'password'} value={values[field.key] ?? ''} onChange={(event) => update(field.key, event.target.value)} placeholder={field.placeholder} autoComplete="off" /><button className="secret-toggle" type="button" onClick={() => setShowSecrets((current) => ({ ...current, [field.key]: !current[field.key] }))} aria-label={showSecrets[field.key] ? `Hide ${field.label}` : `Show ${field.label}`}>{showSecrets[field.key] ? <EyeOff size={16} /> : <Eye size={16} />}</button></div> : <input aria-label={field.label} type={field.type === 'checkbox' ? 'checkbox' : 'text'} checked={field.type === 'checkbox' ? values[field.key] !== 'false' : undefined} value={field.type === 'checkbox' ? undefined : values[field.key] ?? ''} onChange={(event) => update(field.key, field.type === 'checkbox' ? String(event.target.checked) : event.target.value)} placeholder={field.placeholder} />}</Field>)}</div>{testResult && <div className="notice notice-info" style={{ marginTop: 13 }}><Zap size={15} />{testResult}</div>}<div className="connection-actions"><button type="button" className="button button-ghost button-sm" onClick={() => void revoke()} disabled={working || !connection}><Trash2 size={14} /> Revoke</button><div className="connection-actions-right"><Button type="button" variant="secondary" size="sm" onClick={() => void test()} disabled={working || !connection}><PlugZap size={14} /> Test</Button><Button type="submit" size="sm" disabled={working}><Save size={14} /> Save</Button></div></div></form>
}

export function TeamPage() {
  const { role, user } = useAuth()
  const loader = useCallback(() => teamApi.get(), [])
  const resource = useResource(loader, [])
  const [form, setForm] = useState({ name: '', email: '', password: '', role: 'viewer' as 'owner' | 'editor' | 'viewer' })
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const [workingMember, setWorkingMember] = useState<string | null>(null)
  async function add(event: FormEvent) {
    event.preventDefault(); setWorking(true); setError(null); setMessage(null)
    try { await teamApi.addMember(form); setMessage(`${form.name || form.email} added to the team.`); setForm({ name: '', email: '', password: '', role: 'viewer' }); await resource.reload() }
    catch (requestError) { setError(detailMessage(requestError)) }
    finally { setWorking(false) }
  }
  async function updateMember(memberId: string | undefined, nextRole: 'owner' | 'editor' | 'viewer') {
    if (role !== 'owner' || !memberId || memberId === user?.id) return
    setWorkingMember(memberId); setError(null); setMessage(null)
    try { await teamApi.updateMember(memberId, nextRole); setMessage('Team role updated.'); await resource.reload() }
    catch (requestError) { setError(detailMessage(requestError)) }
    finally { setWorkingMember(null) }
  }
  async function removeMember(memberId: string | undefined, name: string) {
    if (role !== 'owner' || !memberId || memberId === user?.id) return
    if (!window.confirm(`Remove ${name || 'this member'} from the team? They will lose access to this workspace.`)) return
    setWorkingMember(memberId); setError(null); setMessage(null)
    try { await teamApi.removeMember(memberId); setMessage('Team member removed.'); await resource.reload() }
    catch (requestError) { setError(detailMessage(requestError)) }
    finally { setWorkingMember(null) }
  }
  return <ResourceStateView resource={resource} empty={<ErrorState message="No team response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => <><div className="grid-2"><Panel padded><div className="panel-header"><div><h2 className="panel-title">Team members</h2><p className="panel-subtitle">Roles control who can change policy, credentials, and members. The API protects the last owner.</p></div><Users size={19} color="#148b89" /></div>{data.items.length ? <div className="team-list">{data.items.map((member) => <div className="team-row" key={member.id ?? member.email}><div><div className="member-name">{member.name || 'Unnamed member'}{member.id === user?.id ? ' (you)' : ''}</div><div className="member-email">{member.email}</div></div><label className="sr-only" htmlFor={`team-role-${member.id ?? member.email}`}>Role for {member.email}</label><select id={`team-role-${member.id ?? member.email}`} aria-label={`Role for ${member.email}`} value={member.role} onChange={(event) => void updateMember(member.id, event.target.value as 'owner' | 'editor' | 'viewer')} disabled={role !== 'owner' || member.id === user?.id || workingMember === member.id || !member.id}><option value="viewer">Viewer</option><option value="editor">Editor</option><option value="owner">Owner</option></select><button type="button" className="button button-ghost button-sm" onClick={() => void removeMember(member.id, member.name || member.email)} disabled={role !== 'owner' || member.id === user?.id || workingMember === member.id || !member.id} title={member.id === user?.id ? 'The current owner cannot remove themselves' : 'Remove this member'}>{workingMember === member.id ? 'Saving…' : 'Remove'}</button></div>)}</div> : <EmptyState icon={<Users size={19} />} title="No members returned" description="The owner account will appear here after the API returns the team roster." />}</Panel><Panel padded><div className="panel-header"><div><h2 className="panel-title">Add a teammate</h2><p className="panel-subtitle">Credentials are sent once to the API and are never echoed in this screen.</p></div><UserPlus size={19} color="#148b89" /></div>{role !== 'owner' && <Notice kind="warning">Only the owner can add or change team members.</Notice>}{message && <div className="mt-20"><Notice kind="success">{message}</Notice></div>}{error && <div className="mt-20"><Notice kind="error">{error}</Notice></div>}<form className="stack-sm" style={{ marginTop: 16 }} onSubmit={(event) => void add(event)}><Field label="Name" required><input value={form.name} onChange={(event) => setForm((current) => ({ ...current, name: event.target.value }))} required disabled={role !== 'owner'} /></Field><Field label="Email" required><input type="email" value={form.email} onChange={(event) => setForm((current) => ({ ...current, email: event.target.value }))} required disabled={role !== 'owner'} /></Field><Field label="Temporary password" required><input type="password" minLength={8} value={form.password} onChange={(event) => setForm((current) => ({ ...current, password: event.target.value }))} required disabled={role !== 'owner'} /></Field><Field label="Role"><select value={form.role} onChange={(event) => setForm((current) => ({ ...current, role: event.target.value as typeof form.role }))} disabled={role !== 'owner'}><option value="viewer">Viewer</option><option value="editor">Editor</option><option value="owner">Owner</option></select></Field><div className="form-actions"><Button type="submit" disabled={working || role !== 'owner'}><UserPlus size={15} /> {working ? 'Adding…' : 'Add member'}</Button></div></form></Panel></div></>}
  </ResourceStateView>
}

const MAX_POSTS_PER_WEEK = 2
const MAX_REFRESHES_PER_WEEK = 1
const MAX_TRACKED_QUESTIONS = 20
const MAX_MONTHLY_BUDGET_DOLLARS = 300
const MAX_MONTHLY_BUDGET_CENTS = MAX_MONTHLY_BUDGET_DOLLARS * 100
const DEFAULT_POLICY: PolicySettings = { enabled: false, allowed_actions: ['metadata'], protected_paths: ['/', '/contact*', '/privacy*', '/terms*', '/checkout*', '/cart*', '/my-account*'], posts_per_week: MAX_POSTS_PER_WEEK, refreshes_per_week: MAX_REFRESHES_PER_WEEK, monthly_budget_cents: MAX_MONTHLY_BUDGET_CENTS, tracked_keywords: [], competitors: [], tracked_questions: [], publish_days: [1, 4], author_id: null }

function BudgetLedger() {
  const siteId=useSiteId()
  const {role}=useAuth()
  const loader=useCallback(()=>budgetsApi.get(siteId),[siteId])
  const resource=useResource(loader,[siteId])
  const [selected,setSelected]=useState('')
  const [actual,setActual]=useState('')
  const [evidence,setEvidence]=useState('')
  const [error,setError]=useState('')
  const [busy,setBusy]=useState(false)
  async function reconcile(event:FormEvent) {
    event.preventDefault();setBusy(true);setError('')
    try {await budgetsApi.settle(siteId,selected,Number(actual),evidence);setSelected('');setActual('');setEvidence('');await resource.reload()}
    catch(error) {setError(detailMessage(error))}
    finally {setBusy(false)}
  }
  return <Panel padded><h2>Provider cost reconciliation</h2><p className="text-small text-muted">If a provider does not return an actual charge, the maximum stays reserved. Reconcile it only from a provider invoice or usage record.</p>
    <ResourceStateView resource={resource} empty={<p>No cost ledger returned.</p>}>{data=><>
      {data.reservations.items.length===0 ? <p>No paid work has been reserved.</p> : <ul aria-label="Provider reservations">{data.reservations.items.map(row=><li key={row.id}>
        {titleCase(row.status)} — reservation ceiling {formatCurrencyCents(row.estimated_cents)};
        {' '}{row.status === 'reserved' ? `held ${formatCurrencyCents(row.estimated_cents)} (not measured spending)` : 'no longer held'};
        {' '}actual charge: {row.actual_cents === null || row.actual_cents === undefined ? 'Unknown — not recorded' : formatCurrencyCents(row.actual_cents)}
      </li>)}</ul>}
      {role==='owner' && data.reservations.items.some(row=>row.status==='reserved') && <form className="stack-sm" onSubmit={event=>void reconcile(event)}>
        <Field label="Reservation"><select required value={selected} onChange={event=>setSelected(event.target.value)}><option value="">Choose a reservation</option>{data.reservations.items.filter(row=>row.status==='reserved').map(row=><option key={row.id} value={row.id}>{row.operation_key} — {formatCurrencyCents(row.estimated_cents)}</option>)}</select></Field>
        <Field label="Actual provider charge (cents)"><input type="number" min="0" step="1" required value={actual} onChange={event=>setActual(event.target.value)}/></Field>
        <Field label="Invoice or usage evidence"><textarea minLength={12} required value={evidence} onChange={event=>setEvidence(event.target.value)}/></Field>
        <Button type="submit" disabled={busy}>Record actual cost</Button>
      </form>}
      {error && <Notice kind="error">{error}</Notice>}
    </>}</ResourceStateView>
  </Panel>
}

type SetupReadinessState = 'ready' | 'needs setup' | 'needs review' | 'not verified' | 'optional' | 'unsupported' | 'degraded'

interface SetupReadinessItem {
  key: string
  title: string
  state: SetupReadinessState
  description: string
  link?: string
  linkLabel?: string
}

const SETUP_READINESS_TONES: Record<SetupReadinessState, 'amber' | 'green' | 'slate' | 'red'> = {
  ready: 'green',
  'needs setup': 'amber',
  'needs review': 'amber',
  'not verified': 'amber',
  optional: 'slate',
  unsupported: 'red',
  degraded: 'red',
}

function sourceReadiness(connection: Connection | undefined, name: string, purpose: string, siteId: string, optional = false): SetupReadinessItem {
  const status = connection?.status?.toLowerCase()
  const connectionPath = `/sites/${siteId}/settings/connections`
  if (!connection || ['needs_connection', 'revoked', 'disconnected'].includes(status ?? '')) {
    return {
      key: name,
      title: name,
      state: optional ? 'optional' : 'needs setup',
      description: optional
        ? `Not connected. ${purpose} will remain unavailable, but this does not block the paused technical pilot.`
        : `Connect and test ${name} before enabling any workflow that depends on it.`,
      link: connectionPath,
      linkLabel: 'Configure connections',
    }
  }
  const readiness = connectionReadiness(connection)
  if (readiness.state === 'unsupported') {
    return {
      key: name,
      title: name,
      state: 'unsupported',
      description: `${readiness.description} ${purpose}`,
      link: connectionPath,
      linkLabel: 'Review connections',
    }
  }
  if (readiness.state === 'degraded') {
    return {
      key: name,
      title: name,
      state: 'degraded',
      description: `${readiness.description} ${purpose}`,
      link: connectionPath,
      linkLabel: 'Review connection',
    }
  }
  if (readiness.state === 'automatic') {
    return {
      key: name,
      title: name,
      state: 'ready',
      description: `The latest connection check is recorded. ${purpose}`,
      link: connectionPath,
      linkLabel: 'Review connection',
    }
  }
  return {
    key: name,
    title: name,
    state: 'needs review',
    description: `${readiness.description} ${purpose}`,
    link: connectionPath,
    linkLabel: 'Review connection',
  }
}

function SetupReadiness({ siteId, settings, sitePaused, globalPause, connections, authorIsVerified }: { siteId: string; settings: PolicySettings; sitePaused: boolean; globalPause: boolean; connections: Connection[]; authorIsVerified: boolean }) {
  const wordpress = connections.find((connection) => connection.kind === 'wordpress')
  const gsc = connections.find((connection) => connection.kind === 'gsc')
  const ga4 = connections.find((connection) => connection.kind === 'ga4')
  const dataForSeo = connections.find((connection) => connection.kind === 'dataforseo')
  const ai = connections.find((connection) => connection.kind === 'ai')
  const safeguardIssues: string[] = []
  if (settings.enabled) safeguardIssues.push('automation is already enabled')
  if (!sitePaused) safeguardIssues.push('the site pause is off')
  if (!globalPause) safeguardIssues.push('the workspace emergency pause is off')
  if (!settings.protected_paths.length) safeguardIssues.push('no protected paths are listed')
  if (settings.monthly_budget_cents <= 0) safeguardIssues.push('the monthly budget is zero')
  if (settings.allowed_actions.includes('publish') && (!settings.author_id || !authorIsVerified)) safeguardIssues.push('publishing has no verified author')
  const safeguards: SetupReadinessItem = safeguardIssues.length
    ? { key: 'pilot-safeguards', title: 'Pilot safeguards', state: 'needs setup', description: `Before a controlled pilot, fix: ${safeguardIssues.join('; ')}.` }
    : { key: 'pilot-safeguards', title: 'Pilot safeguards', state: 'ready', description: 'Automation is off, both pauses are on, protected paths and a budget are present, and any enabled publishing action has a verified author.' }
  const items: SetupReadinessItem[] = [
    { key: 'server', title: 'Always-on server', state: 'not verified', description: 'Confirm HTTPS, the API, web app, queue, workers, scheduler, and browser worker are healthy on the deployment server. This page cannot verify those processes.' },
    { key: 'backups', title: 'Encrypted backups and restore', state: 'not verified', description: 'Configure an encrypted off-site backup with a separate key, then complete and record a restore test. A local page load is not proof of recoverability.' },
    sourceReadiness(wordpress, 'WordPress access', 'Authenticated inventory and governed WordPress workflows can be used.', siteId),
    sourceReadiness(gsc, 'Google Search Console', 'Search queries and landing-page observations can be collected.', siteId, true),
    sourceReadiness(ga4, 'Google Analytics 4', 'Traffic and configured conversion observations can be collected.', siteId, true),
    sourceReadiness(dataForSeo, 'DataForSEO', 'Paid keyword and SERP research can be collected within the site budget.', siteId, true),
    sourceReadiness(ai, 'AI answer provider', 'Provider-scoped answer samples can be collected and clearly labeled.', siteId, true),
    safeguards,
  ]
  const attention = items.filter((item) => !['ready', 'optional'].includes(item.state))
  return <Panel padded>
    <div className="panel-header"><div><h2 id="automation-readiness-title" className="panel-title">Before enabling automation</h2><p className="panel-subtitle">A plain-language handoff for the checks that belong to the application and the checks that still belong to the deployment operator.</p></div><Badge value={attention.length ? 'operator checks pending' : 'ready for review'} tone={attention.length ? 'amber' : 'green'} /></div>
    <Notice kind="warning" title="Do not enable automation yet">Keep the site and workspace pauses on until the “not verified” server and backup checks are complete, WordPress access is tested, and the controlled pilot is explicitly approved. Optional provider connections improve coverage but do not silently authorize publishing.</Notice>
    <ul aria-label="Automation readiness checklist" style={{ listStyle: 'none', margin: '16px 0 0', padding: 0, borderTop: '1px solid var(--line)' }}>
      {items.map((item) => <li key={item.key} style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto', gap: 12, padding: '12px 0', borderBottom: '1px solid var(--line)', alignItems: 'start' }}><div><div className="text-small" style={{ fontWeight: 700 }}>{item.title}</div><div className="text-small text-muted">{item.description}</div>{item.link && <Link className="text-small" to={item.link}>{item.linkLabel}</Link>}</div><Badge value={item.state} tone={SETUP_READINESS_TONES[item.state]} /></li>)}
    </ul>
    <p className="text-small text-muted" style={{ margin: '14px 0 0' }}>“Ready” means this screen has enough application evidence for the item. It is not a ranking promise, a provider guarantee, or proof that the seven-day unattended pilot has passed.</p>
  </Panel>
}

function PolicySummary({ settings, sitePaused, globalPause, site }: { settings: PolicySettings; sitePaused: boolean; globalPause: boolean; site: Site }) {
  const actions = settings.allowed_actions.length ? settings.allowed_actions.map((action) => titleCase(action)).join(', ') : 'No actions allowed'
  const protectedPaths = settings.protected_paths.length ? settings.protected_paths.join(', ') : 'No protected paths listed'
  return <Panel padded>
    <div className="panel-header"><div><h2 className="panel-title">Policy summary</h2><p className="panel-subtitle">Read these guardrails before enabling automation. This summary reflects the values currently loaded in this form.</p></div><Badge value={settings.enabled ? 'enabled' : 'disabled'} /></div>
    <Notice kind={settings.enabled ? 'warning' : 'info'}>{settings.enabled ? 'Automation is enabled, but the API still enforces every pause, permission, fact, and budget check.' : 'Automation is disabled. Review the summary below before an owner enables it.'}</Notice>
    <div className="metric-row"><span>Site</span><strong>{site.name || 'Unnamed site'}</strong></div>
    <div className="metric-row"><span>Automation</span><strong>{settings.enabled ? 'Enabled' : 'Disabled'}</strong></div>
    <div className="metric-row"><span>Site pause</span><strong>{sitePaused ? 'Paused' : 'Not paused'}</strong></div>
    <div className="metric-row"><span>Workspace emergency pause</span><strong>{globalPause ? 'Paused' : 'Not paused'}</strong></div>
    <div className="metric-row"><span>Allowed actions</span><strong className="text-small">{actions}</strong></div>
    <div className="metric-row"><span>Protected paths</span><strong className="text-small">{protectedPaths}</strong></div>
    <div className="metric-row"><span>Cadence</span><strong className="text-small">{settings.posts_per_week} posts / week · {settings.refreshes_per_week} refreshes / week</strong></div>
    <div className="metric-row"><span>Monthly budget</span><strong>{formatCurrencyCents(settings.monthly_budget_cents)}</strong></div>
    <div className="metric-row"><span>Publishing author</span><strong>{settings.author_id ? 'Configured' : 'Not configured'}</strong></div>
  </Panel>
}

type MigrationHistoryStatus = 'imported' | 'rolled_back'
type MigrationFreshness = 'fresh' | 'aging' | 'stale' | 'unknown'

interface MigrationHistoryRow {
  id?: string | number
  kind: 'legacy_import' | 'legacy_rollback'
  data: Record<string, unknown>
  source?: string
  observed_at?: string
  created_at?: string
}

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

function textField(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function countField(data: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = data[key]
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return Math.round(value)
  }
  return null
}

function normalizeMigrationHistory(value: unknown): { rows: MigrationHistoryRow[]; malformed: boolean } {
  const payload = objectRecord(value)
  const items = Array.isArray(value) ? value : payload?.items
  if (!Array.isArray(items)) return { rows: [], malformed: true }

  let malformed = false
  const rows: MigrationHistoryRow[] = []
  for (const item of items) {
    const record = objectRecord(item)
    if (!record) {
      malformed = true
      continue
    }
    if (record.kind !== 'legacy_import' && record.kind !== 'legacy_rollback') continue
    const data = objectRecord(record.data)
    if (!data) {
      malformed = true
      continue
    }
    const id = typeof record.id === 'string' || typeof record.id === 'number' ? record.id : undefined
    rows.push({
      id,
      kind: record.kind,
      data,
      source: textField(record.source),
      observed_at: textField(record.observed_at),
      created_at: textField(record.created_at),
    })
  }
  return { rows, malformed }
}

function migrationHistoryStatus(data: Record<string, unknown>): MigrationHistoryStatus {
  const rollback = objectRecord(data.rollback)
  const states = [
    data.status,
    data.state,
    data.import_status,
    data.migration_status,
    data.rollback_status,
    data.rollback,
    rollback?.status,
    rollback?.state,
  ].map((value) => typeof value === 'string' ? value.trim().toLowerCase().replace(/[-\s]+/g, '_') : '')
  if (data.rolled_back === true || data.rollback === true || Boolean(textField(data.rolled_back_at)) || rollback?.rolled_back === true || states.some((state) => state === 'rolled_back' || state === 'rollback')) return 'rolled_back'
  return 'imported'
}

function migrationFreshness(value: string | undefined, now = Date.now()): MigrationFreshness {
  if (!value) return 'unknown'
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp) || timestamp > now) return 'unknown'
  const age = now - timestamp
  if (age <= 7 * 24 * 60 * 60 * 1000) return 'fresh'
  if (age <= 30 * 24 * 60 * 60 * 1000) return 'aging'
  return 'stale'
}

function migrationFreshnessTone(value: MigrationFreshness): 'green' | 'amber' | 'red' | 'slate' {
  if (value === 'fresh') return 'green'
  if (value === 'aging') return 'amber'
  if (value === 'stale') return 'red'
  return 'slate'
}

function migrationFreshnessLabel(value: MigrationFreshness) {
  return value === 'unknown' ? 'Unknown' : titleCase(value)
}

function migrationHistoryCount(data: Record<string, unknown>) {
  const explicit = countField(data, ['imported_history_count', 'history_count', 'history_records', 'total_history_records'])
  const historyCounts = objectRecord(data.history_counts) ?? {}
  const breakdown = Object.entries(historyCounts)
    .map(([key, value]) => [key, typeof value === 'number' && Number.isFinite(value) && value >= 0 ? Math.round(value) : null] as const)
    .filter((entry): entry is [string, number] => entry[1] !== null)
  const total = explicit ?? (breakdown.length ? breakdown.reduce((sum, [, value]) => sum + value, 0) : null)
  return { total, breakdown }
}

function migrationChecksum(row: MigrationHistoryRow) {
  const explicit = textField(row.data.sha256) ?? textField(row.data.checksum)
  if (explicit) return explicit
  return row.source && /^[a-f0-9]{32,128}$/i.test(row.source) ? row.source : undefined
}

function mergeMigrationHistory(rows: MigrationHistoryRow[]): MigrationHistoryRow[] {
  const imports = rows.filter((row) => row.kind === 'legacy_import')
  const rollbacks = rows.filter((row) => row.kind === 'legacy_rollback')
  const matched = new Set<MigrationHistoryRow>()
  const merged = imports.map((row) => {
    const checksum = migrationChecksum(row)
    const rollback = checksum ? rollbacks.find((candidate) => migrationChecksum(candidate) === checksum) : undefined
    if (!rollback) return row
    matched.add(rollback)
    return {
      ...row,
      data: {
        ...row.data,
        status: 'rolled_back',
        rolled_back_at: rollback.observed_at ?? rollback.created_at,
        rollback: rollback.data,
      },
    }
  })
  const unmatchedRollbacks = rollbacks.filter((row) => !matched.has(row)).map((row) => ({
    ...row,
    data: { ...row.data, status: 'rolled_back' },
  }))
  return [...merged, ...unmatchedRollbacks]
}

function MigrationHistoryRecord({ row, index }: { row: MigrationHistoryRow; index: number }) {
  const status = migrationHistoryStatus(row.data)
  const freshness = migrationFreshness(row.observed_at ?? row.created_at)
  const pages = countField(row.data, ['imported_pages', 'pages_imported'])
  const history = migrationHistoryCount(row.data)
  const archive = textField(row.data.archive) ?? textField(row.data.archive_path)
  const checksum = migrationChecksum(row)
  const statusLabel = status === 'rolled_back' ? 'Import rolled back' : 'Imported historical evidence'
  const recordKey = `${row.id ?? 'legacy-import'}-${row.observed_at ?? row.created_at ?? index}`

  return <div role="listitem" key={recordKey} style={{ border: '1px solid var(--line)', borderRadius: 9, padding: 14 }}>
    <div className="panel-header" style={{ marginBottom: 12 }}><div><strong>{statusLabel}</strong><div className="text-small text-muted">Recorded {formatDateTime(row.observed_at ?? row.created_at)}</div></div><Badge value={statusLabel} tone={status === 'rolled_back' ? 'amber' : 'blue'} /></div>
    <div className="metric-row"><span>Imported pages</span><strong>{pages === null ? 'Not provided' : formatNumber(pages)}</strong></div>
    <div className="metric-row"><span>Historical records</span><strong>{history.total === null ? 'Not provided' : formatNumber(history.total)}</strong></div>
    {history.breakdown.length > 0 && <div className="text-small text-muted" style={{ marginTop: 5 }}>Breakdown: {history.breakdown.map(([key, value]) => `${titleCase(key)}: ${formatNumber(value)}`).join(' · ')}</div>}
    {archive && <div className="text-small text-muted" style={{ marginTop: 9, overflowWrap: 'anywhere' }}>Archive: <code>{archive}</code></div>}
    {checksum && <div className="text-small text-muted" style={{ marginTop: 5, overflowWrap: 'anywhere' }}>Checksum: <code>{checksum}</code></div>}
    <div className="text-small text-muted" style={{ marginTop: 10 }}>Record freshness: <Badge value={migrationFreshnessLabel(freshness)} tone={migrationFreshnessTone(freshness)} /></div>
    {freshness === 'stale' && <div className="text-small text-muted" style={{ marginTop: 8 }}>This migration record is older than 30 days. It remains historical evidence; fresh inventory is required before relying on current coverage.</div>}
    {freshness === 'unknown' && <div className="text-small text-muted" style={{ marginTop: 8 }}>The migration timestamp is missing or invalid. Treat this record as historical evidence only and collect fresh inventory.</div>}
  </div>
}

function MigrationHistoryContent({ value }: { value: unknown }) {
  const { rows, malformed } = normalizeMigrationHistory(value)
  const orderedRows = mergeMigrationHistory(rows).sort((left, right) => {
    const leftTime = Date.parse(left.observed_at ?? left.created_at ?? '')
    const rightTime = Date.parse(right.observed_at ?? right.created_at ?? '')
    return (Number.isFinite(rightTime) ? rightTime : 0) - (Number.isFinite(leftTime) ? leftTime : 0)
  })

  if (malformed && orderedRows.length === 0) {
    return <><Notice kind="error" title="Migration history needs review">The measurements response did not contain a readable legacy import record. No import state is inferred; retry the read before relying on migration history.</Notice><EmptyState title="No readable imported history" description="The current response cannot confirm whether history was imported. Imported history is not proof that the site is optimized." /></>
  }
  if (orderedRows.length === 0) {
    return <EmptyState icon={<History size={20} />} title="No imported history" description="No legacy import record was returned for this site. An empty history is not proof that the site is optimized; fresh inventory is still required." />
  }

  const latestStatus = migrationHistoryStatus(orderedRows[0].data)
  return <>
    {malformed && <Notice kind="warning" title="Some migration records need review">Some legacy import rows were malformed and are not shown. The readable rows below are historical evidence only.</Notice>}
    <Notice kind={latestStatus === 'rolled_back' ? 'warning' : 'info'} title={latestStatus === 'rolled_back' ? 'Import rolled back' : 'Imported historical evidence'}>{latestStatus === 'rolled_back' ? 'The latest readable import is marked rolled back. Its record remains visible for auditability, but it is not active inventory or permission.' : 'Imported history is retained as historical evidence for auditability. It does not grant current permissions or approvals.'} Fresh inventory is required before current decisions or automation. This panel does not indicate that the site is optimized.</Notice>
    <div role="list" aria-label="Migration history records" className="stack-sm" style={{ marginTop: 14 }}>{orderedRows.map((row, index) => <MigrationHistoryRecord key={`${row.id ?? 'legacy-import'}-${index}`} row={row} index={index} />)}</div>
  </>
}

function MigrationHistoryPanel() {
  const siteId = useSiteId()
  const loader = useCallback(() => operationsApi.measurements(siteId, { limit: 200 }), [siteId])
  const resource = useResource(loader, [siteId])

  return <Panel padded>
    <div className="panel-header"><div><h2 className="panel-title">Migration history</h2><p className="panel-subtitle">A read-only record of historical evidence imported from an earlier ForgeSEO installation.</p></div><History size={19} color="#148b89" /></div>
    <Notice kind="info" title="Historical evidence only">Imported approvals do not grant current permissions. Fresh inventory is required before current decisions or automation; this panel never authorizes a write or means the site is optimized.</Notice>
    <div style={{ marginTop: 14 }}><ResourceStateView resource={resource} empty={<ErrorState message="No migration history response was returned. Retry the read before relying on this evidence." onRetry={() => void resource.reload()} />}>
      {(data) => <MigrationHistoryContent value={data} />}
    </ResourceStateView></div>
  </Panel>
}

export function PoliciesPage() {
  const siteId = useSiteId()
  const { role } = useAuth()
  const canEdit = role === 'owner'
  const authorDiscovery = useAuthorDiscovery(siteId)
  const loader = useCallback(async () => { const [policy, settings, site, connections] = await Promise.all([policyApi.get(siteId), settingsApi.get(), sitesApi.get(siteId), connectionsApi.list(siteId)]); return { policy, settings, site, connections } }, [siteId])
  const resource = useResource(loader, [siteId])
  const [settings, setSettings] = useState<PolicySettings>(DEFAULT_POLICY)
  const [sitePaused, setSitePaused] = useState(true)
  const [globalPause, setGlobalPause] = useState(true)
  const [budget, setBudget] = useState('300')
  const [keywords, setKeywords] = useState('')
  const [competitors, setCompetitors] = useState('')
  const [questions, setQuestions] = useState('')
  const [protectedPaths, setProtectedPaths] = useState('')
  const [initialized, setInitialized] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const authorIsVerified = authorDiscovery.isVerified(settings.author_id)
  const postsLimitError = settings.posts_per_week > MAX_POSTS_PER_WEEK ? `The API allows no more than ${MAX_POSTS_PER_WEEK} posts per week.` : undefined
  const refreshesLimitError = settings.refreshes_per_week > MAX_REFRESHES_PER_WEEK ? `The API allows no more than ${MAX_REFRESHES_PER_WEEK} refresh per week.` : undefined
  const trackedQuestions = questions.split('\n').map((item) => item.trim()).filter(Boolean)
  const trackedQuestionsError = trackedQuestions.length > MAX_TRACKED_QUESTIONS
    ? `The API allows no more than ${MAX_TRACKED_QUESTIONS} tracked questions.`
    : undefined
  const monthlyBudgetValue = budget.trim() === '' ? 0 : Number(budget)
  const monthlyBudgetError = budget.trim() !== '' && (!Number.isFinite(monthlyBudgetValue) || monthlyBudgetValue < 0 || monthlyBudgetValue > MAX_MONTHLY_BUDGET_DOLLARS)
    ? `The API allows no more than $${MAX_MONTHLY_BUDGET_DOLLARS} per site per month.`
    : undefined
  const cadenceError = postsLimitError ?? refreshesLimitError ?? trackedQuestionsError

  useEffect(() => {
    if (!resource.data || initialized) return
    const current = { ...DEFAULT_POLICY, ...resource.data.policy.settings }
    setSettings(current)
    setSitePaused(resource.data.site.paused)
    setGlobalPause(resource.data.settings.global_pause)
    setBudget(String((current.monthly_budget_cents / 100).toFixed(2)))
    setKeywords(current.tracked_keywords.join(', ')); setCompetitors(current.competitors.join(', ')); setQuestions(current.tracked_questions.join('\n')); setProtectedPaths(current.protected_paths.join('\n')); setInitialized(true)
  }, [initialized, resource.data])

  useEffect(() => {
    if (trackedQuestionsError) {
      setError(trackedQuestionsError)
    } else {
      setError((current) => current === `The API allows no more than ${MAX_TRACKED_QUESTIONS} tracked questions.` ? null : current)
    }
  }, [trackedQuestionsError])

  function toggleAction(action: string) { if (!canEdit) return; setSettings((current) => ({ ...current, allowed_actions: current.allowed_actions.includes(action) ? current.allowed_actions.filter((item) => item !== action) : [...current.allowed_actions, action] })) }
  function toggleDay(day: number) { if (!canEdit) return; setSettings((current) => ({ ...current, publish_days: current.publish_days.includes(day) ? current.publish_days.filter((item) => item !== day) : [...current.publish_days, day].sort() })) }
  async function save() {
    if (!canEdit) return
    const validationError = monthlyBudgetError ?? cadenceError ?? trackedQuestionsError
    if (validationError) {
      setError(validationError)
      return
    }
    if (!authorIsVerified) {
      setError('The selected publishing author is not verified by the latest complete author check. Refresh discovery or clear the author before saving.')
      return
    }
    setWorking(true); setMessage(null); setError(null)
    try {
      const nextSettings: PolicySettings = { ...settings, monthly_budget_cents: Math.max(0, Math.round(Number(budget || 0) * 100)), tracked_keywords: stringList(keywords), competitors: stringList(competitors), tracked_questions: trackedQuestions, protected_paths: protectedPaths.split('\n').map((item) => item.trim()).filter(Boolean) }
      await policyApi.update(siteId, nextSettings)
      await sitesApi.update(siteId, { paused: sitePaused })
      if (role === 'owner') await settingsApi.update({ global_pause: globalPause })
      setMessage('Policy and pause controls saved as new server state.')
      await resource.reload()
      setInitialized(false)
    } catch (requestError) { setError(detailMessage(requestError)) }
    finally { setWorking(false) }
  }

  return <><div className="stack">{resource.data && <SetupReadiness siteId={siteId} settings={settings} sitePaused={sitePaused} globalPause={globalPause} connections={resource.data.connections.items} authorIsVerified={authorIsVerified} />}{resource.data && <PolicySummary settings={settings} sitePaused={sitePaused} globalPause={globalPause} site={resource.data.site} />}{resource.data && <PublicationScopePanel siteId={siteId} settings={settings} disabled={!canEdit || working || !initialized} onChange={ids => setSettings(current => ({ ...current, publication_article_ids: ids }))} />}{resource.data && !canEdit && <Notice kind="warning" title="Read-only for your role">Only the owner can change policy, pause, and automation controls. The API remains the authority.</Notice>}</div><fieldset disabled={!canEdit} style={{ border: 0, padding: 0, margin: 0 }}><ResourceStateView resource={resource} empty={<ErrorState message="No policy response was returned." onRetry={() => void resource.reload()} />}>
    {() => <><div className="stack"><BudgetLedger /><Panel padded><div className="panel-header"><div><h2 className="panel-title">Pause controls</h2><p className="panel-subtitle">Use the smallest pause that matches the situation. Changes are persisted through the API.</p></div><LockKeyhole size={19} color="#148b89" /></div><div className="toggle-row"><div className="toggle-copy"><strong>Site paused</strong><span>Blocks policy evaluation for this site while keeping observations and reviews available.</span></div><button type="button" className={`switch ${sitePaused ? 'on' : ''}`} aria-label="Toggle site pause" aria-pressed={sitePaused} onClick={() => setSitePaused((value) => !value)}><span /></button></div><div className="toggle-row"><div className="toggle-copy"><strong>Global pause</strong><span>Owner-only emergency control across the workspace.</span></div><button type="button" className={`switch ${globalPause ? 'on' : ''}`} aria-label="Toggle global pause" aria-pressed={globalPause} disabled={role !== 'owner'} onClick={() => setGlobalPause((value) => !value)}><span /></button></div></Panel><Panel padded><div className="panel-header"><div><h2 className="panel-title">Policy & budget</h2><p className="panel-subtitle">Version {resource.data?.policy.version} · append-only settings keep prior decisions auditable.</p></div><Badge value={settings.enabled ? 'enabled' : 'disabled'} /></div><div className="toggle-row"><div className="toggle-copy"><strong>Allow automated workflows</strong><span>Keep this off while you are reviewing the first audit.</span></div><button type="button" className={`switch ${settings.enabled ? 'on' : ''}`} aria-label="Toggle automated workflows" aria-pressed={settings.enabled} onClick={() => setSettings((current) => ({ ...current, enabled: !current.enabled }))}><span /></button></div><div className="divider" /><div className="budget-settings"><Field label="Monthly budget (USD)" hint={`Hard API ceiling: $${MAX_MONTHLY_BUDGET_DOLLARS} per site per month. Current limit ${formatCurrencyCents(settings.monthly_budget_cents)}`} error={monthlyBudgetError}><input id="monthly-budget" type="number" min="0" max={MAX_MONTHLY_BUDGET_DOLLARS} step="0.01" aria-describedby="monthly-budget-limit" aria-invalid={Boolean(monthlyBudgetError)} value={budget} onChange={(event) => setBudget(event.target.value)} /><span id="monthly-budget-limit" className="sr-only">{`The monthly budget may be from $0 to $${MAX_MONTHLY_BUDGET_DOLLARS} per site per month.`}</span></Field><Field label="Posts per week" hint={`API limit: up to ${MAX_POSTS_PER_WEEK} posts per week.`} error={postsLimitError}><input type="number" min="0" max={MAX_POSTS_PER_WEEK} step="1" aria-invalid={Boolean(postsLimitError)} value={settings.posts_per_week} onChange={(event) => setSettings((current) => ({ ...current, posts_per_week: Number(event.target.value) }))} /></Field><Field label="Refreshes per week" hint={`API limit: up to ${MAX_REFRESHES_PER_WEEK} refresh per week.`} error={refreshesLimitError}><input type="number" min="0" max={MAX_REFRESHES_PER_WEEK} step="1" aria-invalid={Boolean(refreshesLimitError)} value={settings.refreshes_per_week} onChange={(event) => setSettings((current) => ({ ...current, refreshes_per_week: Number(event.target.value) }))} /></Field><div style={{ gridColumn: '1 / -1' }}><AuthorDiscoverySelector discovery={authorDiscovery} value={settings.author_id ?? ''} onChange={(value) => setSettings((current) => ({ ...current, author_id: value || null }))} /></div></div><div className="divider" /><Field label="Allowed actions" hint="Metadata is the conservative default. Publishing and refresh stay explicit."><div className="policy-actions">{['metadata', 'publish', 'refresh', 'store_editorial', 'links', 'alt_text'].map((action) => <label className="checkbox-field" key={action}><input type="checkbox" checked={settings.allowed_actions.includes(action)} onChange={() => toggleAction(action)} /><span>{titleCase(action)}</span></label>)}</div></Field><div className="form-grid mt-20"><Field label="Protected paths" hint="One path or wildcard per line."><textarea value={protectedPaths} onChange={(event) => setProtectedPaths(event.target.value)} /></Field><Field label="Tracked keywords" hint="Comma-separated, max 25 by contract."><textarea value={keywords} onChange={(event) => setKeywords(event.target.value)} /></Field><Field label="Competitors" hint="Comma-separated, max 3 by contract."><textarea value={competitors} onChange={(event) => setCompetitors((event.target as HTMLTextAreaElement).value)} /></Field><Field label="Tracked questions" hint="One question per line, max 20 by contract."><textarea value={questions} onChange={(event) => setQuestions(event.target.value)} /></Field></div><Field label="Publish days" hint="Select the allowed weekdays."><div className="day-picker">{[['M', 0], ['T', 1], ['W', 2], ['T', 3], ['F', 4], ['S', 5], ['S', 6]].map(([label, day]) => <label key={`${label}-${day}`}><input type="checkbox" checked={settings.publish_days.includes(day as number)} onChange={() => toggleDay(day as number)} /><span>{label}</span></label>)}</div></Field><div className="form-actions"><Button onClick={() => void save()} disabled={working || !authorIsVerified || Boolean(cadenceError) || Boolean(monthlyBudgetError)}><Save size={15} /> {working ? 'Saving policy…' : 'Save policy controls'}</Button></div></Panel>{message && <Notice kind="success"><ShieldCheck size={16} />{message}</Notice>}{error && <Notice kind="error">{error}</Notice>}</div></>}
  </ResourceStateView></fieldset><div className="stack"><MigrationHistoryPanel /></div></>
}
