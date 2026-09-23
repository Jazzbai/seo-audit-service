import { useEffect } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { ErrorState, LoadingState } from './components/ui'
import { useAuth, useSites } from './context/AppContext'
import { AppShell } from './components/AppShell'
import { LoginPage, BootstrapPage } from './pages/AuthPages'
import { OnboardingPage } from './pages/OnboardingPage'
import { OverviewPage } from './pages/OverviewPage'
import { IssuesPage, PagesPage } from './pages/SeoPages'
import { ArticleEditorPage, ArticlesPage, ContentCalendarPage } from './pages/ContentPages'
import { StorePage } from './pages/StorePage'
import { VisibilityPage } from './pages/VisibilityPage'
import { ActivityPage, IncidentsPage, JobsPage, PublicationsPage, WeeklyReportPage } from './pages/OperationsPages'
import { ConnectionsPage, PoliciesPage, SettingsLayout, TeamPage } from './pages/SettingsPages'
import { BusinessFactsPage } from './pages/BusinessFactsPage'

function FullPageLoading() {
  return <div className="auth-main"><LoadingState label="Preparing your workspace" /></div>
}

function PublicRoute() {
  const { status } = useAuth()
  const { pathname } = useLocation()
  if (status === 'loading') return <FullPageLoading />
  if (status === 'authenticated') return <Navigate to="/" replace />
  if (status === 'uninitialized' && pathname !== '/bootstrap') return <Navigate to="/bootstrap" replace />
  return <Outlet />
}

function AuthenticatedRoute() {
  const { status } = useAuth()
  if (status === 'loading') return <FullPageLoading />
  if (status === 'uninitialized') return <Navigate to="/bootstrap" replace />
  if (status !== 'authenticated') return <Navigate to="/login" replace />
  return <Outlet />
}

function OnboardingRoute() {
  const { status } = useAuth()
  if (status === 'loading') return <FullPageLoading />
  if (status !== 'authenticated') return <Navigate to="/login" replace />
  return <OnboardingPage />
}

function SiteRoute() {
  const { status: authStatus } = useAuth()
  const { sites, status, error, refresh } = useSites()
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const siteId = pathname.split('/')[2]

  useEffect(() => {
    if (status === 'ready' && sites.length === 0 && !pathname.endsWith('/new')) navigate('/sites/new', { replace: true })
    if (status === 'ready' && sites.length > 0 && siteId && !sites.some((site) => site.id === siteId)) navigate(`/sites/${sites[0].id}/overview`, { replace: true })
  }, [navigate, pathname, siteId, sites, status])

  if (authStatus !== 'authenticated' || status === 'loading' || status === 'idle') return <FullPageLoading />
  if (error && sites.length === 0) return <div className="auth-main"><ErrorState message={error} onRetry={() => { void refresh().catch(() => undefined) }} /></div>
  if (!sites.length) return <FullPageLoading />
  return <AppShell />
}

function RootRedirect() {
  const { status: authStatus } = useAuth()
  const { sites, status } = useSites()
  if (authStatus === 'loading') return <FullPageLoading />
  if (authStatus === 'uninitialized') return <Navigate to="/bootstrap" replace />
  if (authStatus !== 'authenticated') return <Navigate to="/login" replace />
  if (status === 'loading' || status === 'idle') return <FullPageLoading />
  if (!sites.length) return <Navigate to="/sites/new" replace />
  return <Navigate to={`/sites/${sites[0].id}/overview`} replace />
}

export default function App() {
  return (
    <Routes>
      <Route element={<PublicRoute />}>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/bootstrap" element={<BootstrapPage />} />
      </Route>
      <Route element={<AuthenticatedRoute />}>
        <Route path="/sites/new" element={<OnboardingRoute />} />
        <Route path="/sites/:siteId" element={<SiteRoute />}>
          <Route index element={<Navigate to="overview" replace />} />
          <Route path="overview" element={<OverviewPage />} />
          <Route path="issues" element={<IssuesPage />} />
          <Route path="pages" element={<PagesPage />} />
          <Route path="content" element={<ContentCalendarPage />} />
          <Route path="content/articles" element={<ArticlesPage />} />
          <Route path="content/new" element={<ArticleEditorPage />} />
          <Route path="content/articles/:articleId" element={<ArticleEditorPage />} />
          <Route path="store" element={<StorePage />} />
          <Route path="visibility" element={<VisibilityPage />} />
          <Route path="activity" element={<ActivityPage />} />
          <Route path="jobs" element={<JobsPage />} />
          <Route path="incidents" element={<IncidentsPage />} />
          <Route path="publications" element={<PublicationsPage />} />
          <Route path="reports/weekly" element={<WeeklyReportPage />} />
          <Route path="settings" element={<SettingsLayout />}>
            <Route index element={<Navigate to="connections" replace />} />
            <Route path="business" element={<BusinessFactsPage />} />
            <Route path="connections" element={<ConnectionsPage />} />
            <Route path="team" element={<TeamPage />} />
            <Route path="policies" element={<PoliciesPage />} />
          </Route>
        </Route>
      </Route>
      <Route path="*" element={<RootRedirect />} />
      <Route path="/" element={<RootRedirect />} />
    </Routes>
  )
}
