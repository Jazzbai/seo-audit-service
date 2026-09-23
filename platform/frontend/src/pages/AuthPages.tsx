import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowRight, CheckCircle2, ShieldCheck } from 'lucide-react'
import { Button, Field, Notice } from '../components/ui'
import { useAuth } from '../context/AppContext'
import { detailMessage } from '../lib/api'

function AuthAside({ bootstrap }: { bootstrap?: boolean }) {
  return (
    <aside className="auth-aside">
      <div className="auth-brand"><Link to="/" className="brand"><span className="brand-mark">✦</span><span>FORGESEO</span></Link></div>
      <div className="auth-quote">
        <div className="auth-quote-mark">“</div>
        <h1>Organic growth, with a conscience.</h1>
        <p>ForgeSEO gives your team a careful operating system for finding, reviewing, and publishing useful work.</p>
      </div>
      <div className="auth-aside-footer">{bootstrap ? 'A calm start for your first site.' : 'Built for teams who want their safeguards visible.'}</div>
    </aside>
  )
}

export function LoginPage() {
  const { signIn } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await signIn(email.trim(), password)
      navigate('/', { replace: true })
    } catch (requestError) {
      setError(detailMessage(requestError))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-layout">
      <AuthAside />
      <main className="auth-main">
        <div className="auth-card">
          <p className="eyebrow">Welcome back</p>
          <h2>Sign in to ForgeSEO</h2>
          <p className="auth-card-intro">Keep your organic work moving without losing sight of the guardrails.</p>
          {error && <div className="mb-20"><Notice kind="error">{error}</Notice></div>}
          <form className="auth-form" onSubmit={submit}>
            <Field label="Work email" required><input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" required placeholder="you@company.com" /></Field>
            <Field label="Password" required><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required placeholder="Your password" /></Field>
            <div className="form-actions"><Button className="button-wide" size="lg" type="submit" disabled={submitting}>{submitting ? 'Signing in…' : 'Sign in'}<ArrowRight size={17} /></Button></div>
          </form>
          <p className="auth-switch">New workspace? <Link to="/bootstrap">Create the first owner account</Link></p>
        </div>
      </main>
    </div>
  )
}

export function BootstrapPage() {
  const { bootstrap } = useAuth()
  const navigate = useNavigate()
  const [form, setForm] = useState({ name: '', email: '', password: '', team_name: '', bootstrap_token: '' })
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const update = (key: keyof typeof form, value: string) => setForm((current) => ({ ...current, [key]: value }))

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const { bootstrap_token, ...account } = form
      await bootstrap(account, bootstrap_token)
      navigate('/sites/new', { replace: true })
    } catch (requestError) {
      setError(detailMessage(requestError))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-layout">
      <AuthAside bootstrap />
      <main className="auth-main">
        <div className="auth-card">
          <p className="eyebrow">Set up your workspace</p>
          <h2>Create the first owner account</h2>
          <p className="auth-card-intro">Start with a small, private workspace. You can add teammates after your first site is connected.</p>
          {error && <div className="mb-20"><Notice kind="error">{error}</Notice></div>}
          <form className="auth-form" onSubmit={submit}>
            <Field label="Your name" required><input value={form.name} onChange={(event) => update('name', event.target.value)} autoComplete="name" required placeholder="Alex Morgan" /></Field>
            <Field label="Work email" required><input type="email" value={form.email} onChange={(event) => update('email', event.target.value)} autoComplete="email" required placeholder="you@company.com" /></Field>
            <Field label="Workspace name" required><input value={form.team_name} onChange={(event) => update('team_name', event.target.value)} required placeholder="Northstar growth team" /></Field>
            <Field label="Password" hint="Use at least 8 characters." required><input type="password" minLength={8} value={form.password} onChange={(event) => update('password', event.target.value)} autoComplete="new-password" required placeholder="A secure password" /></Field>
            <Field label="Deployment setup token" hint="Your server administrator set this in the private deployment environment. It is used only for this first-owner setup; regular sign-in never asks for it." required><input type="password" value={form.bootstrap_token} onChange={(event) => update('bootstrap_token', event.target.value)} autoComplete="off" required placeholder="Paste the one-time setup token" /></Field>
            <div className="notice notice-info"><ShieldCheck size={17} aria-hidden="true" /><div>ForgeSEO starts with global automation paused and asks for connection details only when a workflow needs them.</div></div>
            <div className="form-actions"><Button size="lg" type="submit" disabled={submitting}>{submitting ? 'Creating workspace…' : 'Create workspace'}<ArrowRight size={17} /></Button></div>
          </form>
          <p className="auth-switch"><CheckCircle2 size={14} style={{ verticalAlign: 'middle', marginRight: 4 }} /> Already have access? <Link to="/login">Sign in</Link></p>
        </div>
      </main>
    </div>
  )
}
