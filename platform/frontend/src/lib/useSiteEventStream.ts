import { useEffect, useState } from 'react'
import type { EventRecord, JobProgress, JobProgressKind, JobProgressStage, JobProgressStatus } from '../types'

export type SiteEventStreamStatus = 'disabled' | 'unsupported' | 'connecting' | 'connected' | 'reconnecting'

export interface SiteEventStreamOptions {
  enabled?: boolean
  onEvent?: (event: EventRecord) => void
  onProgress?: (progress: JobProgress) => void
}

function eventsUrl(siteId: string) {
  return `/api/v1/sites/${encodeURIComponent(siteId)}/events`
}

function parseEvent(siteId: string, payload: string): EventRecord | null {
  try {
    const value = JSON.parse(payload) as Partial<EventRecord>
    if (!value || value.id === undefined || value.id === null || typeof value.kind !== 'string' || typeof value.message !== 'string') return null
    if (value.site_id !== undefined && value.site_id !== null && String(value.site_id) !== siteId) return null
    return {
      id: value.id,
      site_id: value.site_id,
      team_id: value.team_id,
      kind: value.kind,
      message: value.message,
      data: value.data,
      created_at: value.created_at,
    }
  } catch {
    return null
  }
}

type UnknownRecord = Record<string, unknown>

function asRecord(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as UnknownRecord : null
}

function stringValue(value: unknown) {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function normalizedToken(value: unknown) {
  return stringValue(value)?.toLowerCase().replace(/[\s-]+/g, '_')
}

function firstValue(sources: UnknownRecord[], keys: string[]) {
  for (const source of sources) {
    for (const key of keys) {
      if (source[key] !== undefined && source[key] !== null) return source[key]
    }
  }
  return undefined
}

function safeKind(value: unknown): JobProgressKind {
  switch (normalizedToken(value)) {
    case 'audit': return 'audit'
    case 'inventory': return 'inventory'
    case 'plan': return 'plan'
    case 'generate': return 'generate'
    case 'publish': return 'publish'
    case 'availability': return 'availability'
    case 'visibility': return 'visibility'
    case 'refresh': return 'refresh'
    case 'full_cycle': return 'full_cycle'
    default: return 'other'
  }
}

function safeStatus(value: unknown): JobProgressStatus {
  switch (normalizedToken(value)) {
    case 'queued':
    case 'pending':
      return 'queued'
    case 'running':
    case 'started':
    case 'in_progress':
    case 'processing':
      return 'running'
    case 'complete':
    case 'completed':
    case 'success':
    case 'succeeded':
    case 'done':
      return 'complete'
    case 'partial':
    case 'complete_with_errors':
      return 'partial'
    case 'failed':
    case 'failure':
    case 'error':
      return 'failed'
    case 'blocked':
    case 'needs_reconciliation':
    case 'ambiguous':
      return 'blocked'
    case 'cancelled':
    case 'canceled':
      return 'cancelled'
    case 'retry':
    case 'retrying':
      return 'retrying'
    case 'needs_connection':
      return 'needs_connection'
    case 'needs_review':
      return 'needs_review'
    default:
      return 'unknown'
  }
}

function safeStage(value: unknown): JobProgressStage | undefined {
  switch (normalizedToken(value)) {
    case 'availability':
    case 'availability_check':
      return 'availability'
    case 'inventory':
    case 'wordpress_inventory':
      return 'inventory'
    case 'audit':
    case 'public_audit':
      return 'public_audit'
    case 'plan':
    case 'content_plan':
    case 'planning':
      return 'content_plan'
    case 'refresh':
    case 'refresh_evaluation':
      return 'refresh_evaluation'
    case 'generate':
    case 'generation':
      return 'generate'
    case 'publish':
    case 'publication':
      return 'publish'
    case 'visibility':
      return 'visibility'
    default:
      return undefined
  }
}

function safeInteger(value: unknown) {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= 1_000_000 ? value : undefined
}

function parseProgress(siteId: string, payload: string): JobProgress | null {
  try {
    const root = asRecord(JSON.parse(payload))
    if (!root) return null

    const data = asRecord(root.data)
    const nestedProgress = asRecord(root.progress)
    const nestedJob = asRecord(root.job)
    const sources = [data, nestedProgress, nestedJob, root].filter((value): value is UnknownRecord => value !== null)
    const eventSiteId = stringValue(firstValue(sources, ['site_id', 'siteId']))
    if (eventSiteId && eventSiteId !== siteId) return null

    const nestedJobId = nestedJob ? firstValue([nestedJob], ['id', 'job_id', 'jobId']) : undefined
    const jobId = stringValue(firstValue(sources, ['job_id', 'jobId'])) ?? stringValue(nestedJobId)
    const statusValue = firstValue(sources, ['status', 'job_status'])
    if (!jobId || !stringValue(statusValue)) return null

    const kindValue = firstValue(sources, ['job_kind', 'jobKind', 'kind'])
    const completed = safeInteger(firstValue(sources, ['completed', 'completed_steps', 'current', 'done']))
    const total = safeInteger(firstValue(sources, ['total', 'total_steps']))
    const stageIndex = safeInteger(firstValue(sources, ['stage_index', 'stageIndex']))
    const stageCount = safeInteger(firstValue(sources, ['stage_count', 'stageCount']))
    const percent = safeInteger(firstValue(sources, ['percent', 'percentage']))

    return {
      site_id: eventSiteId,
      job_id: jobId,
      kind: safeKind(kindValue),
      status: safeStatus(statusValue),
      stage: safeStage(firstValue(sources, ['stage', 'stage_name', 'current_stage', 'phase'])),
      completed,
      total,
      stage_index: stageIndex,
      stage_count: stageCount,
      percent: percent !== undefined && percent <= 100 ? percent : undefined,
      updated_at: stringValue(firstValue(sources, ['updated_at', 'created_at'])),
    }
  } catch {
    return null
  }
}

const progressEventNames = ['job_progress', 'job-progress', 'progress'] as const

/**
 * Subscribe to the authenticated, site-scoped activity stream.
 * EventSource performs the reconnect and Last-Event-ID handling for us.
 */
export function useSiteEventStream(siteId: string, { enabled = true, onEvent, onProgress }: SiteEventStreamOptions) {
  const [status, setStatus] = useState<SiteEventStreamStatus>(enabled ? 'connecting' : 'disabled')

  useEffect(() => {
    if (!enabled || !siteId) {
      setStatus('disabled')
      return
    }
    if (typeof EventSource === 'undefined') {
      setStatus('unsupported')
      return
    }

    setStatus('connecting')
    const source = new EventSource(eventsUrl(siteId), { withCredentials: true })
    const handleOpen = () => setStatus('connected')
    const handleError = () => setStatus('reconnecting')
    const handleActivity = (event: Event) => {
      const message = event as MessageEvent<string>
      const parsed = parseEvent(siteId, message.data)
      if (parsed) onEvent?.(parsed)
    }
    const handleProgress = (event: Event) => {
      const message = event as MessageEvent<string>
      const parsed = parseProgress(siteId, message.data)
      if (parsed) onProgress?.(parsed)
    }

    source.onopen = handleOpen
    source.onerror = handleError
    source.addEventListener('activity', handleActivity)
    if (onProgress) progressEventNames.forEach((eventName) => source.addEventListener(eventName, handleProgress))
    return () => {
      source.removeEventListener('activity', handleActivity)
      if (onProgress) progressEventNames.forEach((eventName) => source.removeEventListener(eventName, handleProgress))
      source.close()
    }
  }, [enabled, onEvent, onProgress, siteId])

  return { status }
}
