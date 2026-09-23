import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { AlertCircle, Check, CircleDashed, Info, LoaderCircle, RefreshCw } from 'lucide-react'
import { titleCase } from '../lib/format'

export function Button({ className = '', variant = 'primary', size = 'md', children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'ghost' | 'danger'; size?: 'sm' | 'md' | 'lg' }) {
  return (
    <button className={`button button-${variant} button-${size} ${className}`} {...props}>
      {children}
    </button>
  )
}

export function Badge({ value, tone }: { value: string; tone?: 'teal' | 'amber' | 'red' | 'slate' | 'blue' | 'green' }) {
  const normalized = value.toLowerCase().replace(/\s+/g, '_')
  const inferred = normalized.includes('connected') || normalized.includes('published') || normalized.includes('passed') || normalized === 'approved' || normalized === 'resolved' || normalized === 'healthy'
    ? 'green'
    : normalized.includes('need') || normalized.includes('pending') || normalized.includes('planned') || normalized.includes('scheduled') || normalized.includes('review') || normalized.includes('warning')
      ? 'amber'
      : normalized.includes('error') || normalized.includes('failed') || normalized.includes('critical') || normalized.includes('blocked') || normalized === 'open'
        ? 'red'
        : normalized.includes('testing') || normalized.includes('running') || normalized.includes('draft')
          ? 'blue'
          : 'slate'
  return <span className={`badge badge-${tone ?? inferred}`}><span className="badge-dot" />{titleCase(value)}</span>
}

export function Panel({ children, className = '', padded = true }: { children: ReactNode; className?: string; padded?: boolean }) {
  return <section className={`panel ${padded ? 'panel-padded' : ''} ${className}`}>{children}</section>
}

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description?: string; actions?: ReactNode }) {
  return (
    <div className="page-header">
      <div>
        {eyebrow && <p className="eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        {description && <p className="page-description">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  )
}

export function Notice({ children, kind = 'info', title }: { children: ReactNode; kind?: 'info' | 'success' | 'warning' | 'error'; title?: string }) {
  const Icon = kind === 'error' ? AlertCircle : kind === 'success' ? Check : kind === 'warning' ? CircleDashed : Info
  return (
    <div className={`notice notice-${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
      <Icon size={17} strokeWidth={2} aria-hidden="true" />
      <div>{title && <strong>{title}</strong>}<div>{children}</div></div>
    </div>
  )
}

export function LoadingState({ label = 'Loading your workspace' }: { label?: string }) {
  return <div className="state-card" role="status"><LoaderCircle className="spin" size={22} aria-hidden="true" /><span>{label}…</span></div>
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state-card state-error" role="alert">
      <AlertCircle size={22} aria-hidden="true" />
      <div><strong>We couldn’t load this view.</strong><span>{message}</span></div>
      {onRetry && <Button variant="secondary" size="sm" onClick={onRetry}><RefreshCw size={15} /> Try again</Button>}
    </div>
  )
}

export function EmptyState({ icon, title, description, action }: { icon?: ReactNode; title: string; description: string; action?: ReactNode }) {
  return (
    <div className="empty-state">
      {icon && <div className="empty-icon">{icon}</div>}
      <h3>{title}</h3>
      <p>{description}</p>
      {action && <div className="empty-action">{action}</div>}
    </div>
  )
}

export function StaleState({ onRefresh }: { onRefresh?: () => void }) {
  return <div className="stale-line"><span>Showing the last successfully loaded view.</span>{onRefresh && <button onClick={onRefresh} type="button">Refresh</button>}</div>
}

export function ProgressBar({ value, tone = 'teal' }: { value: number; tone?: 'teal' | 'amber' | 'red' }) {
  return <div className="progress-track" role="progressbar" aria-label="Budget used" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.max(0,Math.min(100,value))}><span className={`progress-fill progress-${tone}`} style={{ width: `${Math.max(0, Math.min(100, value))}%` }} /></div>
}

export function StatCard({ label, value, detail, icon, tone = 'teal' }: { label: string; value: ReactNode; detail?: ReactNode; icon?: ReactNode; tone?: 'teal' | 'navy' | 'amber' | 'red' }) {
  return (
    <div className={`stat-card stat-${tone}`}>
      <div className="stat-card-top"><span className="stat-label">{label}</span>{icon && <span className="stat-icon">{icon}</span>}</div>
      <div className="stat-value">{value}</div>
      {detail && <div className="stat-detail">{detail}</div>}
    </div>
  )
}

export function Field({ label, hint, error, children, required }: { label: string; hint?: string; error?: string; required?: boolean; children: ReactNode }) {
  return <label className="field"><span className="field-label">{label}{required && <span aria-hidden="true"> *</span>}</span>{children}{hint && <span className="field-hint">{hint}</span>}{error && <span className="field-error">{error}</span>}</label>
}

export function TableShell({ children, caption }: { children: ReactNode; caption?: string }) {
  return <div className="table-wrap"><table>{caption && <caption className="sr-only">{caption}</caption>}{children}</table></div>
}

export function Kicker({ children }: { children: ReactNode }) {
  return <span className="kicker">{children}</span>
}
