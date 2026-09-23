import { useState, type FormEvent, type KeyboardEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Check, Plus, X } from 'lucide-react'
import { Button, Field, Notice, Panel } from '../components/ui'
import { useSites } from '../context/AppContext'
import { connectionsApi, detailMessage, sitesApi } from '../lib/api'
import { stringList } from './shared'

function ChipField({ label, values, onChange, placeholder, hint }: { label: string; values: string[]; onChange: (values: string[]) => void; placeholder: string; hint?: string }) {
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
    if (event.key === 'Backspace' && !draft && values.length) onChange(values.slice(0, -1))
  }
  return <Field label={label} hint={hint}>
    <div className="chip-input">
      {values.map((value) => <span className="chip" key={value}>{value}<button type="button" aria-label={`Remove ${value}`} onClick={() => onChange(values.filter((item) => item !== value))}><X size={12} /></button></span>)}
      <input aria-label={label} value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={keyDown} onBlur={() => add(draft)} placeholder={values.length ? 'Add another' : placeholder} />
      <button type="button" className="icon-button" aria-label={`Add ${label.toLowerCase()}`} onClick={() => add(draft)}><Plus size={14} /></button>
    </div>
  </Field>
}

export function OnboardingPage() {
  const navigate = useNavigate()
  const { refresh } = useSites()
  const [form, setForm] = useState({
    name: '',
    origin: '',
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'America/Chicago',
    language: 'en',
    business_name: '',
    audience: '',
    brand_tone: '',
    locations: [] as string[],
    services: [] as string[],
    products: [] as string[],
    authors: [] as string[],
    confirmed_sources: [] as string[],
  })
  const [wordpress, setWordpress] = useState({ username: '', application_password: '' })
  const [error, setError] = useState<string | null>(null)
  const [createdSiteId, setCreatedSiteId] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const update = (key: keyof typeof form, value: string | string[]) => setForm((current) => ({ ...current, [key]: value }))

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    const username = wordpress.username.trim()
    const applicationPassword = wordpress.application_password.trim()
    if (Boolean(username) !== Boolean(applicationPassword)) {
      setError('Enter both the WordPress username and application password, or leave both blank to connect later.')
      return
    }
    setSubmitting(true)
    let createdId: string | null = null
    let connectionSaved = !username
    try {
      const site = await sitesApi.create({
        name: form.name.trim(), origin: form.origin.trim(), timezone: form.timezone, language: form.language,
        facts: {
          business_name: form.business_name.trim(), audience: form.audience.trim(), locations: form.locations,
          services: form.services, products: form.products,
          authors: form.authors.map((name) => ({ name })), confirmed_sources: form.confirmed_sources,
          language: form.language, brand_tone: form.brand_tone.trim(),
        },
      })
      createdId = site.id
      setCreatedSiteId(createdId)
      if (username && applicationPassword) {
        await connectionsApi.save(site.id, 'wordpress', {
          credentials: { username, application_password: applicationPassword },
          settings: {},
        })
        connectionSaved = true
      }
      const sites = await refresh()
      const created = site?.id ? site : sites.find((item) => item.origin === form.origin.trim()) ?? sites[0]
      if (created?.id) navigate(`/sites/${created.id}/overview`, { replace: true })
      else navigate('/', { replace: true })
    } catch (requestError) {
      setError(createdId && !connectionSaved ? `The site was created, but the WordPress connection could not be saved. ${detailMessage(requestError)}` : createdId ? `The site was created, but setup could not be verified. ${detailMessage(requestError)}` : detailMessage(requestError))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="onboarding-wrap">
      <header className="onboarding-header"><div className="brand" style={{ padding: 0, color: 'var(--navy)' }}><span className="brand-mark">✦</span><span>FORGESEO</span></div><span className="text-small text-muted">Step 1 of 1 · Site setup</span></header>
      <main className="onboarding-content">
        <div className="onboarding-intro"><p className="eyebrow">Bring a site into focus</p><h1>Tell ForgeSEO what good work looks like.</h1><p>These facts stay close to every audit and editorial check. Start with what you know; you can refine it in Policies later.</p></div>
        {error && <div className="mb-20"><Notice kind="error">{error}</Notice></div>}
        <div className="onboarding-grid">
          <Panel className="onboarding-card">
            <form onSubmit={submit}>
              <div className="panel-header"><div><h2 className="panel-title">Site basics</h2><p className="panel-subtitle">We’ll use this to identify the site and localize reports.</p></div></div>
              <div className="form-grid">
                <Field label="Site name" required><input value={form.name} onChange={(event) => update('name', event.target.value)} required placeholder="Northstar Dental" /></Field>
                <Field label="Site origin" hint="Include https://" required><input type="url" value={form.origin} onChange={(event) => update('origin', event.target.value)} required placeholder="https://northstardental.com" /></Field>
                <Field label="Timezone" required><input value={form.timezone} onChange={(event) => update('timezone', event.target.value)} required placeholder="America/Chicago" /></Field>
                <Field label="Language" required><select value={form.language} onChange={(event) => update('language', event.target.value)}><option value="en">English</option><option value="es">Spanish</option><option value="fr">French</option></select></Field>
              </div>
              <div className="divider" />
              <div className="panel-header"><div><h2 className="panel-title">Business facts</h2><p className="panel-subtitle">A grounded brief helps the system know what it should not invent.</p></div></div>
              <div className="form-grid">
                <Field label="Business name" required><input value={form.business_name} onChange={(event) => update('business_name', event.target.value)} required placeholder="Northstar Dental" /></Field>
                <Field label="Primary audience" required><input value={form.audience} onChange={(event) => update('audience', event.target.value)} required placeholder="Families in Austin" /></Field>
                <Field label="Brand tone" hint="Optional, for editorial review"><input value={form.brand_tone} onChange={(event) => update('brand_tone', event.target.value)} placeholder="Clear, reassuring, expert" /></Field>
                <div />
                <ChipField label="Locations" values={form.locations} onChange={(value) => update('locations', value)} placeholder="Austin, TX" hint="Press Enter after each location." />
                <ChipField label="Services" values={form.services} onChange={(value) => update('services', value)} placeholder="Family dentistry" />
                <ChipField label="Products" values={form.products} onChange={(value) => update('products', value)} placeholder="Night guards" />
                <ChipField label="Authors" values={form.authors} onChange={(value) => update('authors', value)} placeholder="Dr. Alex Morgan" />
                <ChipField label="Confirmed sources" values={form.confirmed_sources} onChange={(value) => update('confirmed_sources', value)} placeholder="https://example.com/about" hint="URLs or internal source labels you trust." />
              </div>
              <div className="divider" />
              <div className="panel-header"><div><h2 className="panel-title">WordPress connection <span className="text-small text-muted">Optional</span></h2><p className="panel-subtitle">Save the connection now so ForgeSEO can verify authenticated inventory after setup. Leave both fields blank to connect later from Settings.</p></div></div>
              <div className="form-grid">
                <Field label="WordPress username" hint="Use the WordPress user that is allowed to read and edit the intended site content."><input autoComplete="username" value={wordpress.username} onChange={(event) => setWordpress((current) => ({ ...current, username: event.target.value }))} placeholder="wp-editor" /></Field>
                <Field label="Application password" hint="Sent to the API over the authenticated setup request and stored encrypted; it is never shown after saving."><input autoComplete="new-password" type="password" value={wordpress.application_password} onChange={(event) => setWordpress((current) => ({ ...current, application_password: event.target.value }))} placeholder="Paste the WordPress application password" /></Field>
              </div>
              <Notice kind="info" title="No live content changes">Saving this connection only stores access for later capability checks. It does not publish, edit, or delete WordPress content.</Notice>
              {createdSiteId && <Notice kind="warning" title="Site created; finish connection setup">The site exists, but its WordPress connection needs attention. Open Settings to retry without creating another site. <Link to={`/sites/${createdSiteId}/settings/connections`}>Open connection settings</Link></Notice>}
              <div className="form-actions"><Button variant="secondary" type="button" onClick={() => navigate('/')} disabled={submitting}>Cancel</Button><Button size="lg" type="submit" disabled={submitting || Boolean(createdSiteId)}>{submitting ? 'Creating site…' : createdSiteId ? 'Site created' : 'Create site'}<Check size={16} /></Button></div>
            </form>
          </Panel>
          <aside className="policy-preview">
            <p className="eyebrow">Default safety posture</p>
            <h3>Paused until you say go.</h3>
            <p>Your first site starts with reviewable suggestions. Nothing publishes or changes a live page from this setup form.</p>
            {['Metadata suggestions only', 'Protected paths stay protected', 'Two posts per week by default', 'Monthly budget starts at $300', 'Connections are added when needed'].map((item) => <div className="policy-check" key={item}><Check size={15} />{item}</div>)}
          </aside>
        </div>
      </main>
    </div>
  )
}
