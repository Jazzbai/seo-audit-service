import { useCallback } from 'react'
import { Link } from 'react-router-dom'
import { ArrowUpRight, Box, ExternalLink, FileSearch, ShoppingBag, Tag } from 'lucide-react'
import { Badge, EmptyState, ErrorState, Notice, PageHeader, Panel, TableShell } from '../components/ui'
import { connectionsApi, pagesApi, storeApi } from '../lib/api'
import { formatDateTime, titleCase, truncate } from '../lib/format'
import type { Candidate, Connection, Finding, PageRecord } from '../types'
import { ResourceStateView, useResource, useSiteId } from './shared'

type StoreKind = 'product' | 'category'
type SurfaceState = 'read-only' | 'review-only' | 'needs-review' | 'needs-connection' | 'unsupported' | 'error'

interface StoreData {
  inventory: PageRecord[]
  inventoryTotal: number
  products: PageRecord[]
  categories: PageRecord[]
  findings: Finding[]
  candidates: Candidate[]
  connection?: Connection
}

interface StateDescription {
  label: SurfaceState
  description: string
}

type InventoryFreshness = {
  label: 'Within daily cadence' | 'Stale' | 'Freshness unknown'
  tone: 'teal' | 'amber'
  description: string
}

const INVENTORY_DAILY_CADENCE_MS = 24 * 60 * 60 * 1000

/**
 * Classify only the age of the connector observation. A missing, invalid, or
 * future timestamp is deliberately unknown; it must never be presented as
 * current inventory. The 24-hour threshold matches the daily reconciliation
 * cadence and is not an optimization or data-quality claim.
 */
function inventoryFreshness(lastSeenAt?: string | null, now = Date.now()): InventoryFreshness {
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

  if (timestamp < now - INVENTORY_DAILY_CADENCE_MS) {
    return {
      label: 'Stale',
      tone: 'amber',
      description: 'Observed more than 24 hours ago; refresh inventory before relying on this record.',
    }
  }

  return {
    label: 'Within daily cadence',
    tone: 'teal',
    description: 'Observed within the 24-hour inventory cadence. This reflects timestamp age only, not data quality or optimization.',
  }
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function flag(value: unknown, key: string) {
  return record(value)[key] === true
}

function list(value: unknown, key: string) {
  const result = record(value)[key]
  return Array.isArray(result) ? result : []
}

function kindOf(page: PageRecord): StoreKind {
  const resourceType = (page.resource_type ?? '').toLowerCase()
  return ['category', 'categories', 'product_category', 'product_categories'].includes(resourceType) ? 'category' : 'product'
}

function connectionState(connection?: Connection): StateDescription {
  const status = connection?.status?.toLowerCase()
  if (!connection || !status || ['needs_connection', 'revoked', 'disconnected'].includes(status)) {
    return { label: 'needs-connection', description: 'Connect WooCommerce before ForgeSEO can verify live catalog access.' }
  }
  if (['error', 'failed', 'failure'].includes(status)) {
    return { label: 'error', description: 'The latest WooCommerce connection check failed. Review the connection and retry before relying on catalog data.' }
  }
  if (status !== 'connected') {
    return { label: 'needs-review', description: 'Run a successful WooCommerce connection test before relying on this capability.' }
  }
  const capabilities = record(connection.capabilities)
  if (Object.keys(capabilities).length === 0) {
    return { label: 'needs-review', description: 'The connection exists, but its WooCommerce capabilities have not been verified yet.' }
  }
  if (!flag(capabilities, 'woocommerce_api')) {
    return { label: 'unsupported', description: 'The current connection does not expose the WooCommerce REST catalog API.' }
  }
  return { label: 'read-only', description: 'The catalog is being inspected. Commerce data remains protected from this surface.' }
}

function inventoryState(connection: Connection | undefined, kind: StoreKind): StateDescription {
  const base = connectionState(connection)
  if (base.label !== 'read-only') return base
  const capabilities = record(connection?.capabilities)
  const resource = record(capabilities[kind === 'product' ? 'products' : 'categories'])
  if (flag(resource, 'read')) {
    return { label: 'read-only', description: `${kind === 'product' ? 'Product' : 'Category'} records can be inspected; this does not grant permission to change commerce fields.` }
  }
  return { label: 'unsupported', description: `The current connection does not expose ${kind === 'product' ? 'product' : 'category'} inventory access.` }
}

function seoState(connection: Connection | undefined, kind: StoreKind): StateDescription {
  const base = connectionState(connection)
  if (base.label === 'needs-connection' || base.label === 'needs-review' || base.label === 'unsupported' || base.label === 'error') return base

  const capabilities = record(connection?.capabilities)
  const inventory = record(capabilities[kind === 'product' ? 'products' : 'categories'])
  if (!flag(inventory, 'read')) return { label: 'unsupported', description: 'Review the catalog connection before evaluating SEO opportunities for this resource.' }

  const seo = record(capabilities.seo)
  const resourceTypes = list(seo, 'resource_types').map(String)
  const routeDetected = kind === 'product'
    ? resourceTypes.length === 0 || resourceTypes.includes('product')
    : resourceTypes.includes('category')
  if (!routeDetected) {
    return { label: 'unsupported', description: `No verified ${kind === 'product' ? 'product' : 'category'} SEO metadata route is available.` }
  }
  const writableFields = list(seo, 'writable_fields')
  if (flag(seo, 'write') && writableFields.length > 0) {
    return { label: 'review-only', description: 'A narrow SEO metadata route is verified, but each candidate still requires policy and editorial review.' }
  }
  if (flag(seo, 'read')) {
    return { label: 'review-only', description: 'SEO observations can be reviewed, but no verified SEO writer is available for this resource.' }
  }
  return { label: 'unsupported', description: 'The current connection does not expose a verified SEO metadata capability for this resource.' }
}

function resourceName(page: PageRecord | undefined, kind?: StoreKind) {
  if (page?.title) return page.title
  return kind === 'category' ? 'Untitled category' : 'Untitled product'
}

function StoreInventoryTable({ kind, items }: { kind: StoreKind; items: PageRecord[] }) {
  if (!items.length) {
    return <EmptyState icon={kind === 'category' ? <Tag size={20} /> : <ShoppingBag size={20} />} title={`No ${kind} records yet`} description={`No ${kind} records were returned by the current WooCommerce inventory response.`} />
  }
  return <TableShell caption={`${kind === 'category' ? 'Product category' : 'Product'} inventory`}><thead><tr><th>{kind === 'category' ? 'Category' : 'Product'}</th><th>Inventory access</th><th>Editorial coverage</th><th>Last seen and freshness</th><th /></tr></thead><tbody>{items.map((item) => { const freshness = inventoryFreshness(item.last_seen_at); return <tr key={item.id}><td><div className="table-primary">{resourceName(item, kind)}</div>{item.url ? <a className="table-secondary table-url" href={item.url} target="_blank" rel="noreferrer">{item.url}</a> : <div className="table-secondary">No public URL recorded</div>}</td><td><Badge value="read-only" /></td><td><Badge value={item.managed ? 'managed' : 'inventory only'} /></td><td><div className="text-muted">{formatDateTime(item.last_seen_at)}</div><Badge value={freshness.label} tone={freshness.tone} /><div className="table-secondary">{freshness.description}</div></td><td className="text-right">{item.url && <a className="link-button" href={item.url} target="_blank" rel="noreferrer">Open <ExternalLink size={12} style={{ verticalAlign: 'middle' }} /></a>}</td></tr> })}</tbody></TableShell>
}

function StoreFindingTable({ findings, pages }: { findings: Finding[]; pages: Map<string, PageRecord> }) {
  if (!findings.length) {
    return <EmptyState icon={<FileSearch size={20} />} title="No store findings recorded" description="No findings tied to the current product or category inventory were returned. This does not mean the store is fully optimized." />
  }
  return <TableShell caption="Product and category findings"><thead><tr><th>Finding</th><th>Resource</th><th>Severity</th><th>Status</th><th>Last seen</th></tr></thead><tbody>{findings.map((finding) => { const page = finding.page_id ? pages.get(finding.page_id) : undefined; return <tr key={finding.id}><td><div className="table-primary">{finding.title}</div><div className="table-secondary">{finding.code} · {truncate(typeof finding.details?.summary === 'string' ? finding.details.summary : finding.key, 92)}</div></td><td className="text-muted">{resourceName(page, page ? kindOf(page) : undefined)}</td><td><Badge value={finding.severity} /></td><td><Badge value={finding.status} /></td><td className="text-muted">{formatDateTime(finding.last_seen_at)}</td></tr> })}</tbody></TableShell>
}

function StoreCandidateTable({ candidates, pages, access }: { candidates: Candidate[]; pages: Map<string, PageRecord>; access: StateDescription }) {
  if (!candidates.length) {
    return <EmptyState icon={<Box size={20} />} title="No store opportunities yet" description="No product or category candidate changes were returned. An empty queue does not mean the store is fully optimized." />
  }
  return <TableShell caption="Product and category opportunities"><thead><tr><th>Opportunity</th><th>Resource</th><th>Current</th><th>Suggested</th><th>Access</th></tr></thead><tbody>{candidates.map((candidate) => { const page = pages.get(candidate.page_id); const detail = record(candidate.details); const reasons = list(detail, 'review_only_reasons').map(String).filter(Boolean); return <tr key={candidate.id}><td><div className="table-primary">{titleCase(candidate.field)}</div><div className="table-secondary">{candidate.status}</div>{reasons.length > 0 && <div className="text-small" style={{ marginTop: 7, color: '#8a5a00' }}><strong>Review only:</strong> {reasons[0]}</div>}</td><td>{page?.url ? <a className="table-secondary table-url" href={page.url} target="_blank" rel="noreferrer">{resourceName(page, kindOf(page))}</a> : <span className="text-muted">{resourceName(page)}</span>}</td><td><div className="diff-box">{truncate(candidate.before_value, 100)}</div></td><td><div className="diff-box after">{truncate(candidate.after_value, 100)}</div></td><td><Badge value={access.label} /></td></tr> })}</tbody></TableShell>
}

export function StorePage() {
  const siteId = useSiteId()
  const loader = useCallback(async (): Promise<StoreData> => {
    const [inventory, findings, candidates, connections] = await Promise.all([
      storeApi.products(siteId, { limit: 200 }),
      pagesApi.findings(siteId, { limit: 200 }),
      pagesApi.candidates(siteId, { limit: 200 }),
      connectionsApi.list(siteId),
    ])
    const storeItems = inventory.items.filter((item) => ['product', 'products', 'category', 'categories', 'product_category', 'product_categories'].includes((item.resource_type ?? '').toLowerCase()))
    const pages = new Map(storeItems.map((item) => [item.id, item]))
    return {
      inventory: storeItems,
      inventoryTotal: inventory.total,
      products: storeItems.filter((item) => kindOf(item) === 'product'),
      categories: storeItems.filter((item) => kindOf(item) === 'category'),
      findings: findings.items.filter((finding) => finding.page_id ? pages.has(finding.page_id) : false),
      candidates: candidates.items.filter((candidate) => pages.has(candidate.page_id)),
      connection: connections.items.find((connection) => connection.kind === 'woocommerce'),
    }
  }, [siteId])
  const resource = useResource(loader, [siteId])

  return <ResourceStateView resource={resource} empty={<ErrorState message="No store response was returned." onRetry={() => void resource.reload()} />}>
    {(data) => {
      const pages = new Map(data.inventory.map((item) => [item.id, item]))
      const connection = connectionState(data.connection)
      const productInventory = inventoryState(data.connection, 'product')
      const categoryInventory = inventoryState(data.connection, 'category')
      const productSeo = seoState(data.connection, 'product')
      const categorySeo = seoState(data.connection, 'category')
      const opportunityAccess = connection.label === 'needs-connection' || connection.label === 'needs-review' || connection.label === 'unsupported' || connection.label === 'error'
        ? connection
        : { label: 'review-only' as SurfaceState, description: 'Candidates are recommendations until policy and editorial review authorize a narrow SEO change. Commerce fields stay protected.' }
      const inventoryIsPartial = data.inventoryTotal > data.inventory.length
      return <>
        <PageHeader eyebrow="Channels" title="Woo store" description="Review product and category records, findings, and narrow SEO opportunities returned by the store connector. ForgeSEO does not expose commerce editing here." actions={<Link to={`/sites/${siteId}/settings/connections`} className="button button-secondary">Manage Woo connection <ArrowUpRight size={15} /></Link>} />
        <Notice kind="info" title="Store data stays separate.">Prices, inventory, SKUs, checkout, and other commercial fields are not part of this editorial surface and are never written by these controls.</Notice>
        <div className="mt-20"><Notice kind={connection.label === 'error' || connection.label === 'unsupported' ? 'error' : connection.label === 'needs-connection' || connection.label === 'needs-review' ? 'warning' : 'info'} title={`WooCommerce: ${titleCase(connection.label)}`}>{connection.description}{data.connection?.error ? ` ${data.connection.error}` : ''}</Notice></div>
        {inventoryIsPartial && <div className="mt-20"><Notice kind="warning" title="Inventory coverage is partial">Showing {data.inventory.length} of {data.inventoryTotal} catalog records from the latest response. Findings and opportunities below only cover the records returned here; an empty queue does not mean the store is fully optimized.</Notice></div>}
        <div className="mt-20"><Notice kind="info" title="Inventory freshness">Freshness uses the daily inventory cadence: records observed more than 24 hours ago are stale; missing or invalid timestamps remain unknown.</Notice></div>

        <div className="grid-2 mt-20"><Panel padded><div className="panel-header"><div><h2 className="panel-title">Capability coverage</h2><p className="panel-subtitle">These labels describe what the current connection can verify, not permission to edit commerce data.</p></div><Badge value={connection.label} /></div><div className="metric-row"><span>Product inventory</span><strong><Badge value={productInventory.label} /></strong></div><div className="metric-row"><span>Category inventory</span><strong><Badge value={categoryInventory.label} /></strong></div><div className="metric-row"><span>Product SEO opportunities</span><strong><Badge value={productSeo.label} /></strong></div><div className="metric-row"><span>Category SEO opportunities</span><strong><Badge value={categorySeo.label} /></strong></div><p className="text-small text-muted" style={{ marginTop: 14 }}>Only verified SEO metadata routes can ever become eligible for a separate approval workflow. Prices, stock, SKUs, orders, payments, and customer records remain protected.</p></Panel><Panel padded><div className="panel-header"><div><h2 className="panel-title">Inventory summary</h2><p className="panel-subtitle">Stored records from the latest response; this is not a complete-coverage claim.</p></div><Box size={18} color="#148b89" /></div><div className="metric-row"><span>Records returned</span><strong>{data.inventoryTotal}</strong></div><div className="metric-row"><span>Products</span><strong>{data.products.length}</strong></div><div className="metric-row"><span>Categories</span><strong>{data.categories.length}</strong></div><div className="metric-row"><span>Findings</span><strong>{data.findings.length}</strong></div><div className="metric-row"><span>Opportunities</span><strong>{data.candidates.length}</strong></div></Panel></div>

        {data.inventory.length ? <div className="grid-2 mt-20"><Panel padded={false}><div style={{ padding: '22px 22px 0' }}><div className="panel-header"><div><h2 className="panel-title">Products</h2><p className="panel-subtitle">Read-only inventory and editorial coverage.</p></div><Badge value={`${data.products.length} shown`} /></div></div><StoreInventoryTable kind="product" items={data.products} /></Panel><Panel padded={false}><div style={{ padding: '22px 22px 0' }}><div className="panel-header"><div><h2 className="panel-title">Categories</h2><p className="panel-subtitle">Read-only product taxonomy inventory.</p></div><Badge value={`${data.categories.length} shown`} /></div></div><StoreInventoryTable kind="category" items={data.categories} /></Panel></div> : <div className="mt-20"><Panel padded><EmptyState icon={<ShoppingBag size={20} />} title="No store records yet" description="Connect WooCommerce and run inventory before product or category records can appear. ForgeSEO will not invent products or categories in this view." action={<Link to={`/sites/${siteId}/settings/connections`} className="button button-primary button-sm">Open connections</Link>} /></Panel></div>}

        <div className="grid-2 mt-20"><Panel padded={false}><div style={{ padding: '22px 22px 0' }}><div className="panel-header"><div><h2 className="panel-title">Store findings</h2><p className="panel-subtitle">Only findings tied to the current product or category inventory are shown.</p></div><Badge value={`${data.findings.length} shown`} /></div></div><StoreFindingTable findings={data.findings} pages={pages} /></Panel><Panel padded={false}><div style={{ padding: '22px 22px 0' }}><div className="panel-header"><div><h2 className="panel-title">SEO opportunities</h2><p className="panel-subtitle">Recommendations are review-only here; use Issues for the site-wide approval workflow.</p></div><Badge value={opportunityAccess.label} /></div></div><StoreCandidateTable candidates={data.candidates} pages={pages} access={opportunityAccess} />{data.candidates.length > 0 && <div style={{ padding: '0 22px 22px' }}><Link to={`/sites/${siteId}/issues`} className="link-button">Review all opportunities <ArrowUpRight size={14} style={{ verticalAlign: 'middle' }} /></Link></div>}</Panel></div>

        <div className="grid-2 mt-20"><Panel padded><div className="panel-header"><div><h2 className="panel-title">Editorial lane</h2><p className="panel-subtitle">Safe store work starts with a real product or category record.</p></div><Box size={18} color="#148b89" /></div><div className="metric-row"><span>Supported scope</span><strong>SEO metadata only</strong></div><div className="metric-row"><span>Commerce fields</span><strong>Protected</strong></div><div className="metric-row"><span>Candidate workflow</span><strong>Review before action</strong></div></Panel><Panel padded><div className="panel-header"><div><h2 className="panel-title">Need a connection?</h2><p className="panel-subtitle">You can connect WooCommerce separately from WordPress editorial access.</p></div></div><Link to={`/sites/${siteId}/settings/connections`} className="link-button">Configure WooCommerce <ArrowUpRight size={14} style={{ verticalAlign: 'middle' }} /></Link></Panel></div>
      </>
    }}
  </ResourceStateView>
}
