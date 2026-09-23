import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { ApiError, authApi, detailMessage, sitesApi, type AuthContextValue } from '../lib/api'
import type { Site } from '../types'

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthContextValue['user']>(null)
  const [team, setTeam] = useState<AuthContextValue['team']>(null)
  const [role, setRole] = useState<AuthContextValue['role']>(null)
  const [status, setStatus] = useState<AuthContextValue['status']>('loading')

  const applyPayload = (payload: Awaited<ReturnType<typeof authApi.me>>) => {
    setUser(payload.user)
    setTeam(payload.team)
    setRole(payload.role)
    setStatus('authenticated')
  }

  const refresh = async () => {
    try {
      const payload = await authApi.me()
      applyPayload(payload)
      return payload
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setUser(null)
        setTeam(null)
        setRole(null)
        setStatus('unauthenticated')
        return null
      }
      throw error
    }
  }

  useEffect(() => {
    let active = true
    void (async () => {
      try {
        const authStatus = await authApi.status()
        if (!active) return
        if (!authStatus.initialized) {
          setStatus('uninitialized')
          return
        }
        await refresh()
      } catch {
        if (active) setStatus('unauthenticated')
      }
    })()
    return () => {
      active = false
    }
  }, [])

  const value = useMemo<AuthContextValue>(() => ({
    user,
    team,
    role,
    status,
    signIn: async (email, password) => {
      const payload = await authApi.login({ email, password })
      applyPayload(payload)
      return payload
    },
    bootstrap: async (body, bootstrapToken) => {
      const payload = await authApi.bootstrap(body, bootstrapToken)
      applyPayload(payload)
      return payload
    },
    signOut: async () => {
      await authApi.logout()
      setUser(null)
      setTeam(null)
      setRole(null)
      setStatus('unauthenticated')
    },
    refresh,
  }), [role, status, team, user])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}

interface SitesContextValue {
  sites: Site[]
  status: 'idle' | 'loading' | 'ready' | 'error'
  error: string | null
  refresh: () => Promise<Site[]>
}

const SitesContext = createContext<SitesContextValue | null>(null)

export function SitesProvider({ children }: { children: ReactNode }) {
  const { status: authStatus } = useAuth()
  const [sites, setSites] = useState<Site[]>([])
  const [status, setStatus] = useState<SitesContextValue['status']>('idle')
  const [error, setError] = useState<string | null>(null)

  const refresh = async () => {
    setStatus('loading')
    setError(null)
    try {
      const response = await sitesApi.list()
      setSites(response.items)
      setStatus('ready')
      return response.items
    } catch (requestError) {
      setStatus('error')
      setError(detailMessage(requestError))
      throw requestError
    }
  }

  useEffect(() => {
    if (authStatus === 'authenticated') void refresh().catch(() => undefined)
    if (authStatus !== 'authenticated') {
      setSites([])
      setStatus('idle')
      setError(null)
    }
  }, [authStatus])

  const value = useMemo(() => ({ sites, status, error, refresh }), [error, sites, status])
  return <SitesContext.Provider value={value}>{children}</SitesContext.Provider>
}

export function useSites() {
  const context = useContext(SitesContext)
  if (!context) throw new Error('useSites must be used inside SitesProvider')
  return context
}

export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <SitesProvider>{children}</SitesProvider>
    </AuthProvider>
  )
}
