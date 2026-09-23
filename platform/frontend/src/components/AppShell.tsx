import { useEffect, useMemo, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate, useParams } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  BookOpenText,
  CalendarDays,
  ChevronDown,
  ClipboardCheck,
  FileText,
  Globe2,
  History,
  LayoutDashboard,
  LifeBuoy,
  LogOut,
  Menu,
  PanelLeftClose,
  PlugZap,
  Settings2,
  ShoppingBag,
  SlidersHorizontal,
  Sparkles,
  Users,
  X,
} from 'lucide-react'
import { useAuth, useSites } from '../context/AppContext'
import { titleCase } from '../lib/format'

interface NavItem {
  label: string
  path: string
  icon: typeof LayoutDashboard
  end?: boolean
}

function navItems(siteId: string): Array<{ label?: string; items: NavItem[] }> {
  const base = `/sites/${siteId}`
  return [
    { items: [{ label: 'Overview', path: `${base}/overview`, icon: LayoutDashboard, end: true }] },
    { label: 'Growth & SEO', items: [
      { label: 'Issues', path: `${base}/issues`, icon: AlertTriangle },
      { label: 'Pages', path: `${base}/pages`, icon: Globe2 },
    ] },
    { label: 'Content', items: [
      { label: 'Calendar', path: `${base}/content`, icon: CalendarDays },
      { label: 'Articles', path: `${base}/content/articles`, icon: FileText },
    ] },
    { label: 'Channels', items: [
      { label: 'Store', path: `${base}/store`, icon: ShoppingBag },
      { label: 'Visibility', path: `${base}/visibility`, icon: Sparkles },
    ] },
    { label: 'Operations', items: [
      { label: 'Activity', path: `${base}/activity`, icon: Activity },
      { label: 'Jobs', path: `${base}/jobs`, icon: History },
      { label: 'Incidents', path: `${base}/incidents`, icon: LifeBuoy },
      { label: 'Publications', path: `${base}/publications`, icon: ClipboardCheck },
      { label: 'Weekly report', path: `${base}/reports/weekly`, icon: BarChart3 },
    ] },
    { label: 'Workspace', items: [
      { label: 'Connections', path: `${base}/settings/connections`, icon: PlugZap },
      { label: 'Team', path: `${base}/settings/team`, icon: Users },
      { label: 'Policies & budget', path: `${base}/settings/policies`, icon: SlidersHorizontal },
    ] },
  ]
}

function SiteSwitcher({ siteId, closeMenu }: { siteId: string; closeMenu: () => void }) {
  const navigate = useNavigate()
  const { sites, status } = useSites()
  const site = sites.find((item) => item.id === siteId)
  return (
    <div className="site-switcher">
      <div className="site-switcher-label">Active site</div>
      {status === 'loading' ? <div className="site-switcher-empty">Loading sites…</div> : sites.length ? (
        <label>
          <span className="sr-only">Choose a site</span>
          <select value={siteId} onChange={(event) => { closeMenu(); navigate(`/sites/${event.target.value}/overview`) }}>
            {sites.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
          </select>
        </label>
      ) : <div className="site-switcher-empty">No sites yet</div>}
      {site && <div className="site-switcher-empty" title={site.origin}>{site.origin.replace(/^https?:\/\//, '')}</div>}
    </div>
  )
}

function Sidebar({ siteId, open, closeMenu }: { siteId: string; open: boolean; closeMenu: () => void }) {
  const { user, role, signOut } = useAuth()
  const navigate = useNavigate()
  const sections = useMemo(() => navItems(siteId), [siteId])
  return (
    <>
      {open && <button className="sidebar-backdrop" type="button" aria-label="Close navigation" onClick={closeMenu} />}
      <aside className={`sidebar ${open ? 'open' : ''}`} aria-label="Primary navigation">
        <Link to={`/sites/${siteId}/overview`} className="brand" onClick={closeMenu}><span className="brand-mark">✦</span><span>FORGESEO</span></Link>
        <SiteSwitcher siteId={siteId} closeMenu={closeMenu} />
        <nav className="sidebar-nav" aria-label="Primary navigation">
          {sections.map((section, index) => <div key={section.label ?? index}>
            {section.label && <div className="nav-section-label">{section.label}</div>}
            {section.items.map((item) => {
              const Icon = item.icon
              return <NavLink key={item.path} to={item.path} end={item.end} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`} onClick={closeMenu}><Icon size={16} strokeWidth={1.9} /><span>{item.label}</span></NavLink>
            })}
          </div>)}
        </nav>
        <div className="sidebar-bottom">
          <div className="pause-tile"><strong>Automation guardrails</strong>{'Policy and budget controls are active on every change.'}</div>
          <div className="sidebar-footer">
            <div className="user-chip"><span className="avatar">{user?.name?.slice(0, 1).toUpperCase() ?? '?'}</span><span>{user?.name ?? 'Workspace user'}</span></div>
            <button className="icon-button" title="Sign out" aria-label="Sign out" onClick={() => void signOut().then(() => navigate('/login'))}><LogOut size={15} /></button>
          </div>
          <div className="sidebar-footer"><span>{role ? titleCase(role) : 'Workspace'}</span><PanelLeftClose size={14} /></div>
        </div>
      </aside>
    </>
  )
}

export function AppShell() {
  const { siteId = '' } = useParams()
  const { sites } = useSites()
  const [menuOpen, setMenuOpen] = useState(false)
  const location = useLocation()
  const site = sites.find((item) => item.id === siteId)
  const current = titleCase(location.pathname.split('/').filter(Boolean).slice(-1)[0] ?? 'overview')

  useEffect(() => setMenuOpen(false), [location.pathname])

  return (
    <div className="app-shell">
      <Sidebar siteId={siteId} open={menuOpen} closeMenu={() => setMenuOpen(false)} />
      <div className="main-shell">
        <header className="mobile-topbar">
          <button className="icon-button" type="button" onClick={() => setMenuOpen(true)} aria-label="Open navigation"><Menu size={20} /></button>
          <Link to={`/sites/${siteId}/overview`} className="mobile-brand"><span className="brand-mark">✦</span>FORGESEO</Link>
          <Link className="icon-button" to={`/sites/${siteId}/settings/connections`} aria-label="Open settings"><Settings2 size={18} /></Link>
        </header>
        <main id="main-content" className="content-wrap wide">
          <div className="breadcrumb"><span>{site?.name ?? 'Workspace'}</span><ChevronDown size={12} /><strong>{current}</strong></div>
          <Outlet />
        </main>
      </div>
    </div>
  )
}
