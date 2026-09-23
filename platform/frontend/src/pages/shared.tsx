import { useCallback, useEffect, useState, type DependencyList, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'
import { ErrorState, LoadingState, StaleState } from '../components/ui'
import { detailMessage } from '../lib/api'

export function useSiteId() {
  const { siteId = '' } = useParams()
  return siteId
}

export interface ResourceState<T> {
  data: T | null
  loading: boolean
  error: string | null
  stale: boolean
  reload: () => Promise<T | undefined>
}

export function useResource<T>(loader: () => Promise<T>, deps: DependencyList): ResourceState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [stale, setStale] = useState(false)

  const reload = useCallback(async () => {
    setLoading(data === null)
    setError(null)
    if (data !== null) setStale(true)
    try {
      const next = await loader()
      setData(next)
      setStale(false)
      setLoading(false)
      return next
    } catch (requestError) {
      const message = detailMessage(requestError)
      setError(message)
      setLoading(false)
      if (data === null) setData(null)
      return undefined
    }
    // loader and the captured data are intentionally supplied by each page's dependency list.
  }, [data, loader])

  useEffect(() => {
    let active = true
    void (async () => {
      setLoading(true)
      setError(null)
      try {
        const next = await loader()
        if (!active) return
        setData(next)
        setStale(false)
        setLoading(false)
      } catch (requestError) {
        if (!active) return
        setError(detailMessage(requestError))
        setLoading(false)
      }
    })()
    return () => { active = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, loading, error, stale, reload }
}

export function ResourceStateView<T>({ resource, children, empty }: { resource: ResourceState<T>; children: (data: T) => ReactNode; empty?: ReactNode }) {
  if (resource.loading && !resource.data) return <LoadingState />
  if (resource.error && !resource.data) return <ErrorState message={resource.error} onRetry={() => void resource.reload()} />
  if (!resource.data) return <>{empty ?? <div />}</>
  return <>{resource.stale && <StaleState onRefresh={() => void resource.reload()} />}{resource.error && <div className="mt-20"><ErrorState message={resource.error} onRetry={() => void resource.reload()} /></div>}{children(resource.data)}</>
}

export function listFrom<T>(value: { items: T[] } | T[]) {
  return Array.isArray(value) ? value : value.items
}

export function stringList(value: string) {
  return value.split(',').map((item) => item.trim()).filter(Boolean)
}

export function safeValue(value: unknown, fallback = '—') {
  if (value === null || value === undefined || value === '') return fallback
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}
