import { useCallback, useEffect, useState, type FormEvent, type KeyboardEvent } from 'react'
import { Plus, Save, ShieldCheck, Users, X } from 'lucide-react'
import { Badge, Button, EmptyState, ErrorState, Field, Notice, PageHeader, Panel } from '../components/ui'
import { detailMessage, sitesApi } from '../lib/api'
import { useAuth } from '../context/AppContext'
import type { BusinessFacts, Site } from '../types'
import { ResourceStateView, useResource, useSiteId } from './shared'

interface EditableAuthor {
  name: string
  id: string
  email: string
  original?: Record<string, unknown>
}

interface BusinessFactsForm {
  business_name: string
  audience: string
  language: string
  timezone: string
  locations: string[]
  services: string[]
  products: string[]
  authors: EditableAuthor[]
  confirmed_sources: string[]
}

function emptyForm(): BusinessFactsForm {
  return {
    business_name: '',
    audience: '',
    language: '',
    timezone: '',
    locations: [],
    services: [],
    products: [],
    authors: [],
    confirmed_sources: [],
  }
}

function stringValue(value: unknown) {
  return typeof value === 'string' ? value : ''
}

function textValue(value: unknown) {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : ''
}

function stringList(value: unknown) {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => {
      if (typeof item === 'string') return item.trim()
      if (item && typeof item === 'object') {
        const record = item as Record<string, unknown>
        return stringValue(record.url || record.title || record.name).trim()
      }
      return ''
    })
    .filter(Boolean)
}

function sourceEntries(value: unknown): Array<string | Record<string, unknown>> {
  if (!Array.isArray(value)) return []
  const entries: Array<string | Record<string, unknown>> = []
  for (const item of value) {
    if (typeof item === 'string' && item.trim()) entries.push(item.trim())
    else if (item && typeof item === 'object') entries.push(item as Record<string, unknown>)
  }
  return entries
}

function sourceUrl(value: string | Record<string, unknown>) {
  if (typeof value === 'string') return value
  return stringValue(value.url || value.source_url || value.link || value.title).trim()
}

function authorsValue(value: unknown): EditableAuthor[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (typeof item === 'string') {
      const name = item.trim()
      return name ? [{ name, id: '', email: '' }] : []
    }
    if (!item || typeof item !== 'object') return []
    const record = item as Record<string, unknown>
    const author = {
      name: stringValue(record.name).trim(),
      id: textValue(record.id).trim(),
      email: textValue(record.email).trim(),
      original: { ...record },
    }
    return author.name || author.id || author.email ? [author] : []
  })
}

function formFromSite(site: Site): BusinessFactsForm {
  const facts = site.facts ?? {}
  return {
    business_name: stringValue(facts.business_name),
    audience: stringValue(facts.audience),
    language: stringValue(facts.language) || stringValue(site.language),
    timezone: stringValue(site.timezone),
    locations: stringList(facts.locations),
    services: stringList(facts.services),
    products: stringList(facts.products),
    authors: authorsValue(facts.authors),
    confirmed_sources: stringList(facts.confirmed_sources),
  }
}

function cleanList(values: string[]) {
  return values.map((value) => value.trim()).filter(Boolean)
}

function ChipEditor({ label, values, onChange, placeholder, hint, disabled = false }: { label: string; values: string[]; onChange: (values: string[]) => void; placeholder: string; hint?: string; disabled?: boolean }) {
  const [draft, setDraft] = useState('')

  function add(value: string) {
    const next = value.trim()
    if (next && !values.includes(next)) onChange([...values, next])
    setDraft('')
  }

  function keyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault()
      add(draft)
    }
    if (event.key === 'Backspace' && !draft && values.length && !disabled) onChange(values.slice(0, -1))
  }

  return <Field label={label} hint={hint}>
    <div className="chip-input">
      {values.map((value) => <span className="chip" key={value}>{value}<button type="button" aria-label={`Remove ${value}`} disabled={disabled} onClick={() => onChange(values.filter((item) => item !== value))}><X size={12} /></button></span>)}
      <input aria-label={label} value={draft} disabled={disabled} onChange={(event) => setDraft(event.target.value)} onKeyDown={keyDown} onBlur={() => { if (!disabled) add(draft) }} placeholder={values.length ? 'Add another' : placeholder} />
      <button type="button" className="icon-button" aria-label={`Add ${label.toLowerCase()}`} disabled={disabled} onClick={() => add(draft)}><Plus size={14} /></button>
    </div>
  </Field>
}

function AuthorEditor({ authors, onChange, disabled }: { authors: EditableAuthor[]; onChange: (authors: EditableAuthor[]) => void; disabled: boolean }) {
  function update(index: number, key: keyof EditableAuthor, value: string) {
    onChange(authors.map((author, authorIndex) => authorIndex === index ? { ...author, [key]: value } : author))
  }

  function addAuthor() {
    onChange([...authors, { name: '', id: '', email: '' }])
  }

  return <div className="field field-full">
    <span className="field-label">Real authors</span>
    <span className="field-hint">Use an actual connector author name and optional ID or email. Do not add placeholders.</span>
    {!authors.length && <EmptyState icon={<Users size={19} />} title="No author records" description="No real authors are stored for this site yet." action={<Button type="button" variant="secondary" size="sm" onClick={addAuthor} disabled={disabled}><Plus size={14} /> Add author</Button>} />}
    {authors.map((author, index) => <div className="author-editor-row" key={`author-${index}`}>
      <Field label={`Author ${index + 1} name`} required><input aria-label={`Author ${index + 1} name`} value={author.name} disabled={disabled} onChange={(event) => update(index, 'name', event.target.value)} placeholder="Real person or connector author" /></Field>
      <Field label={`Author ${index + 1} ID`} hint="Optional"><input aria-label={`Author ${index + 1} ID`} value={author.id} disabled={disabled} onChange={(event) => update(index, 'id', event.target.value)} placeholder="Connector author ID" /></Field>
      <Field label={`Author ${index + 1} email`} hint="Optional"><input aria-label={`Author ${index + 1} email`} type="email" value={author.email} disabled={disabled} onChange={(event) => update(index, 'email', event.target.value)} placeholder="author@example.com" /></Field>
      <Button type="button" variant="ghost" size="sm" aria-label={`Remove author ${index + 1}`} disabled={disabled} onClick={() => onChange(authors.filter((_, authorIndex) => authorIndex !== index))}><X size={14} /> Remove</Button>
    </div>)}
    {!!authors.length && <Button type="button" variant="secondary" size="sm" onClick={addAuthor} disabled={disabled} style={{ marginTop: 12 }}><Plus size={14} /> Add author</Button>}
  </div>
}

function FactStatus({ label, confirmed, detail }: { label: string; confirmed: boolean; detail: string }) {
  return <div className="metric-row"><div><strong>{label}</strong><div className="text-small text-muted">{detail}</div></div><Badge value={confirmed ? 'provided' : 'missing'} tone={confirmed ? 'green' : 'amber'} /></div>
}

export function BusinessFactsPage() {
  const siteId = useSiteId()
  const { role } = useAuth()
  const canEdit = role === 'owner'
  const loader = useCallback(() => sitesApi.get(siteId), [siteId])
  const resource = useResource(loader, [siteId])
  const [form, setForm] = useState<BusinessFactsForm>(emptyForm)
  const [loadedSiteId, setLoadedSiteId] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!resource.data || resource.data.id !== siteId || loadedSiteId === resource.data.id) return
    setForm(formFromSite(resource.data))
    setLoadedSiteId(resource.data.id)
  }, [loadedSiteId, resource.data])

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!canEdit || !resource.data) return
    setMessage(null)
    setError(null)
    const incompleteAuthor = form.authors.findIndex((author) => (author.id.trim() || author.email.trim()) && !author.name.trim())
    if (incompleteAuthor >= 0) {
      setError(`Add a name for author ${incompleteAuthor + 1} before saving.`)
      return
    }
    setSaving(true)
    const authors = form.authors
      .map((author) => ({ name: author.name.trim(), ...(author.id.trim() ? { id: author.id.trim() } : {}), ...(author.email.trim() ? { email: author.email.trim() } : {}) }))
      .filter((author) => author.name)
    const originalSources = sourceEntries(resource.data.facts?.confirmed_sources)
    const originalByUrl = new Map(originalSources.map((entry) => [sourceUrl(entry), entry]))
    const sourceValues = cleanList(form.confirmed_sources)
    const preservedUnlabeled = originalSources.filter((entry) => !sourceUrl(entry))
    const savedSources = [...preservedUnlabeled, ...sourceValues.map((url) => originalByUrl.get(url) ?? url)]
    const facts: BusinessFacts = {
      ...(resource.data.facts ?? {}),
      business_name: form.business_name.trim(),
      audience: form.audience.trim(),
      language: form.language.trim(),
      locations: cleanList(form.locations),
      services: cleanList(form.services),
      products: cleanList(form.products),
      authors,
      confirmed_sources: savedSources,
    }
    try {
      await sitesApi.update(siteId, { facts, language: form.language.trim(), timezone: form.timezone.trim() })
      const verified = await resource.reload()
      if (!verified) {
        setError('The update was accepted, but the follow-up read could not verify the saved site.')
        return
      }
      setForm(formFromSite(verified))
      setMessage('Business facts saved. The API returned the updated site state.')
    } catch (requestError) {
      setError(detailMessage(requestError))
    } finally {
      setSaving(false)
    }
  }

  return <ResourceStateView resource={resource} empty={<ErrorState message="No site response was returned." onRetry={() => void resource.reload()} />}>
    {(site) => {
      const storedFacts = site.facts ?? {}
      const storedSources = stringList(storedFacts.confirmed_sources)
      const storedAuthors = authorsValue(storedFacts.authors)
      const statusItems = [
        { label: 'Business name', confirmed: !!stringValue(storedFacts.business_name).trim(), detail: stringValue(storedFacts.business_name).trim() || 'Not supplied' },
        { label: 'Primary audience', confirmed: !!stringValue(storedFacts.audience).trim(), detail: stringValue(storedFacts.audience).trim() || 'Not supplied' },
        { label: 'Language', confirmed: !!stringValue(storedFacts.language || site.language).trim(), detail: stringValue(storedFacts.language || site.language).trim() || 'Not supplied' },
        { label: 'Timezone', confirmed: !!site.timezone.trim(), detail: site.timezone.trim() || 'Not supplied' },
        { label: 'Locations', confirmed: stringList(storedFacts.locations).length > 0, detail: stringList(storedFacts.locations).length ? `${stringList(storedFacts.locations).length} stored` : 'None stored' },
        { label: 'Services', confirmed: stringList(storedFacts.services).length > 0, detail: stringList(storedFacts.services).length ? `${stringList(storedFacts.services).length} stored` : 'None stored' },
        { label: 'Products', confirmed: stringList(storedFacts.products).length > 0, detail: stringList(storedFacts.products).length ? `${stringList(storedFacts.products).length} stored` : 'None stored' },
        { label: 'Real authors', confirmed: storedAuthors.some((author) => author.name.trim()), detail: storedAuthors.length ? `${storedAuthors.length} record${storedAuthors.length === 1 ? '' : 's'} stored; connector validation still required` : 'None stored' },
        { label: 'Source references', confirmed: storedSources.length > 0, detail: storedSources.length ? `${storedSources.length} stored reference${storedSources.length === 1 ? '' : 's'}; content not independently attested` : 'None stored' },
      ]
      const confirmedCount = statusItems.filter((item) => item.confirmed).length
      return <>
        <PageHeader eyebrow="Workspace / Business" title="Business facts" description="Review and edit only the site facts you know. These values are sent to the API and used to keep editorial work grounded." actions={<Badge value={canEdit ? 'owner access' : 'read only'} tone={canEdit ? 'teal' : 'slate'} />} />
        {!canEdit && <div className="mb-20"><Notice kind="warning" title="Read-only for your role">Only the site owner can change business facts. The API remains the authority for this permission.</Notice></div>}
        {message && <div className="mb-20"><Notice kind="success"><ShieldCheck size={16} />{message}</Notice></div>}
        {error && <div className="mb-20"><Notice kind="error">{error}</Notice></div>}
        <div className="grid-2">
          <Panel padded>
            <div className="panel-header"><div><h2 className="panel-title">Fact status</h2><p className="panel-subtitle">Provided means a value is stored by the API. It is not an independent verification; disputed or missing facts remain review work.</p></div><Badge value={`${confirmedCount}/${statusItems.length} provided`} /></div>
            {!confirmedCount && <EmptyState icon={<ShieldCheck size={19} />} title="No provided facts are stored yet" description="Start with facts you can verify. Empty fields remain missing until you save them." />}
            <div>{statusItems.map((item) => <FactStatus key={item.label} {...item} />)}</div>
          </Panel>
          <Panel padded>
            <div className="panel-header"><div><h2 className="panel-title">Site record</h2><p className="panel-subtitle">This page reads the current site record before allowing an edit.</p></div></div>
            <div className="metric-row"><span>Site</span><strong>{site.name || 'Unnamed site'}</strong></div>
            <div className="metric-row"><span>Origin</span><strong className="text-small">{site.origin || 'Not supplied'}</strong></div>
            <div className="metric-row"><span>Current automation state</span><Badge value={site.paused ? 'paused' : 'active'} /></div>
            <Notice kind="info">Source references are recorded as owner-provided references. They do not make an unverified claim true by themselves.</Notice>
          </Panel>
        </div>
        <div className="mt-20">
          <Panel padded>
            <div className="panel-header"><div><h2 className="panel-title">Editable facts</h2><p className="panel-subtitle">Save a complete snapshot of the fields below. Existing unedited fact keys are preserved.</p></div><Badge value={canEdit ? 'editable' : 'view only'} tone={canEdit ? 'teal' : 'slate'} /></div>
            <form onSubmit={(event) => void save(event)}>
              <div className="form-grid">
                <Field label="Business name"><input value={form.business_name} disabled={!canEdit} onChange={(event) => setForm((current) => ({ ...current, business_name: event.target.value }))} placeholder="Only enter the verified business name" /></Field>
                <Field label="Primary audience"><input value={form.audience} disabled={!canEdit} onChange={(event) => setForm((current) => ({ ...current, audience: event.target.value }))} placeholder="Only enter the audience you serve" /></Field>
                <Field label="Language" hint="Use the site's primary language code or label."><input value={form.language} disabled={!canEdit} onChange={(event) => setForm((current) => ({ ...current, language: event.target.value }))} placeholder="e.g. en" /></Field>
                <Field label="Timezone" hint="Use an IANA timezone such as America/Chicago."><input value={form.timezone} disabled={!canEdit} onChange={(event) => setForm((current) => ({ ...current, timezone: event.target.value }))} placeholder="e.g. America/Chicago" /></Field>
                <ChipEditor label="Locations" values={form.locations} onChange={(locations) => setForm((current) => ({ ...current, locations }))} placeholder="Add a verified location" hint="Press Enter after each location." disabled={!canEdit} />
                <ChipEditor label="Services" values={form.services} onChange={(services) => setForm((current) => ({ ...current, services }))} placeholder="Add a verified service" hint="Press Enter after each service." disabled={!canEdit} />
                <ChipEditor label="Products" values={form.products} onChange={(products) => setForm((current) => ({ ...current, products }))} placeholder="Add a verified product" hint="Press Enter after each product." disabled={!canEdit} />
                <ChipEditor label="Source references" values={form.confirmed_sources} onChange={(confirmed_sources) => setForm((current) => ({ ...current, confirmed_sources }))} placeholder="Add a trusted URL or source label" hint="Only add references you have actually confirmed." disabled={!canEdit} />
                <AuthorEditor authors={form.authors} onChange={(authors) => setForm((current) => ({ ...current, authors }))} disabled={!canEdit} />
              </div>
              <div className="form-actions"><Button type="submit" disabled={saving || !canEdit}><Save size={15} /> {saving ? 'Saving facts…' : canEdit ? 'Save business facts' : 'Owner access required'}</Button></div>
            </form>
          </Panel>
        </div>
      </>
    }}
  </ResourceStateView>
}
