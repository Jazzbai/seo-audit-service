import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Save, ShieldCheck, Send, PlugZap } from 'lucide-react'
import { Button, Field, Notice } from './ui'
import { connectionsApi, detailMessage, jobsApi, notificationsApi } from '../lib/api'
import { formatDateTime } from '../lib/format'
import type { Connection, ConnectionScopeReview, Job } from '../types'

interface GraphForm {
  tenant_id: string
  client_id: string
  client_secret: string
  sender: string
  recipients: string
  digest_enabled: boolean
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function formFromConnection(connection?: Connection): GraphForm {
  const settings = {
    ...record(connection?.capabilities?.settings),
    ...record(connection?.safe_fields),
    ...record(connection?.settings),
  }
  const recipients = settings.recipients
  return {
    tenant_id: String(settings.tenant_id ?? ''),
    client_id: String(settings.client_id ?? ''),
    client_secret: '',
    sender: String(settings.sender ?? ''),
    recipients: Array.isArray(recipients) ? recipients.map(String).join(', ') : String(recipients ?? ''),
    digest_enabled: settings.digest_enabled === true,
  }
}

function formKey(value: GraphForm) {
  return JSON.stringify(value)
}

function scopeReviewFrom(connection?: Connection): ConnectionScopeReview | null {
  const review = connection?.capabilities?.scope_review
  if (!review || review.kind !== 'owner_attested_exchange_rbac' || !review.reviewed_at || !review.reviewer_id || !review.configuration_sha256) return null
  return review
}

const SCOPE_REVIEW_TTL_MS = 7 * 24 * 60 * 60 * 1000

function scopeReviewExpiry(review: ConnectionScopeReview): Date | null {
  const reviewedAt = Date.parse(review.reviewed_at)
  return Number.isFinite(reviewedAt) ? new Date(reviewedAt + SCOPE_REVIEW_TTL_MS) : null
}

type NotificationTestStatus = 'idle' | 'submitting' | 'queued' | 'running' | 'accepted' | 'failed' | 'blocked' | 'outcome_unknown'

function notificationTestStatus(job: Job): Exclude<NotificationTestStatus, 'idle' | 'submitting'> {
  const status = job.status.toLowerCase()
  const resultStatus = String(job.result?.status ?? '').toLowerCase()
  if (resultStatus === 'failed') return 'failed'
  if (resultStatus === 'blocked') return 'blocked'
  if (status === 'queued') return 'queued'
  if (status === 'running') return 'running'
  if (status === 'failed') return 'failed'
  if (status === 'blocked') return 'blocked'
  if (status === 'complete' && resultStatus === 'accepted') return 'accepted'
  return 'outcome_unknown'
}

function testStatusMessage(status: NotificationTestStatus) {
  switch (status) {
    case 'submitting': return { kind: 'info' as const, title: 'Submitting one test request', body: 'The API has not returned a job handle yet. No delivery is claimed.' }
    case 'queued': return { kind: 'info' as const, title: 'Test email job queued', body: 'The job is queued. This does not mean the message was sent or delivered.' }
    case 'running': return { kind: 'info' as const, title: 'Test email job running', body: 'The job is still running. No delivery is claimed, and another send is locked while the outcome is pending.' }
    case 'accepted': return { kind: 'warning' as const, title: 'Graph accepted the test request', body: 'The completed job reports accepted by Graph. Delivery is still unconfirmed; an intended recipient must confirm receipt below.' }
    case 'failed': return { kind: 'error' as const, title: 'Test email job failed', body: 'The job returned a failure. Review its result before requesting another test.' }
    case 'blocked': return { kind: 'warning' as const, title: 'Test email job blocked', body: 'The API blocked the request. No successful send is claimed.' }
    case 'outcome_unknown': return { kind: 'warning' as const, title: 'Test email outcome unknown', body: 'The final result could not be confirmed. No new send will be started; retry with the same idempotency key or check the existing job.' }
    default: return null
  }
}

function parsedRecipients(value: string) {
  return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean).slice(0, 10)
}

function recipientCount(value: string) {
  return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean).length
}

type WorkingAction = 'save' | 'authentication' | 'scope' | 'send' | 'receipt' | null

export function MicrosoftGraphConnectionCard({ connection, siteId, canEdit, onChanged }: {
  connection?: Connection
  siteId: string
  canEdit: boolean
  onChanged: (message: string) => Promise<void>
}) {
  const [form, setForm] = useState<GraphForm>(() => formFromConnection(connection))
  const [baseline, setBaseline] = useState(() => formKey(formFromConnection(connection)))
  const [scopeReview, setScopeReview] = useState<ConnectionScopeReview | null>(() => scopeReviewFrom(connection))
  const [scopeInvalidated, setScopeInvalidated] = useState(false)
  const [scopeMailbox, setScopeMailbox] = useState(false)
  const [scopeNoUnscoped, setScopeNoUnscoped] = useState(false)
  const [scopeEvidence, setScopeEvidence] = useState('')
  const [working, setWorking] = useState<WorkingAction>(null)
  const [error, setError] = useState<string | null>(null)
  const [authenticationMessage, setAuthenticationMessage] = useState<string | null>(null)
  const [notificationJob, setNotificationJob] = useState<Job | null>(null)
  const [notificationStatus, setNotificationStatus] = useState<NotificationTestStatus>('idle')
  const [sendIdempotencyKey, setSendIdempotencyKey] = useState<string | null>(null)
  const [receivedConfirmation, setReceivedConfirmation] = useState(false)
  const [receiptConfirmed, setReceiptConfirmed] = useState(false)
  const [receiptNotes, setReceiptNotes] = useState('')

  useEffect(() => {
    const next = formFromConnection(connection)
    setForm(next)
    setBaseline(formKey(next))
    if (!scopeInvalidated) setScopeReview(scopeReviewFrom(connection))
  }, [connection, siteId, scopeInvalidated])

  const dirty = formKey(form) !== baseline
  const recipients = useMemo(() => parsedRecipients(form.recipients), [form.recipients])
  const tooManyRecipients = recipientCount(form.recipients) > 10
  const currentScopeReview = scopeInvalidated ? null : scopeReview
  const reviewExpiry = currentScopeReview ? scopeReviewExpiry(currentScopeReview) : null
  const scopeReviewExpired = Boolean(currentScopeReview && (!reviewExpiry || reviewExpiry.getTime() <= Date.now()))
  const hasMailbox = Boolean(form.sender.trim() && recipients.length)
  const scopeVerified = currentScopeReview?.kind === 'owner_attested_exchange_rbac' && !scopeReviewExpired
  const hasUnresolvedTest = ['submitting', 'queued', 'running', 'outcome_unknown'].includes(notificationStatus)
    || (notificationStatus === 'accepted' && !receivedConfirmation)
  const canStartTest = ['idle', 'failed', 'blocked'].includes(notificationStatus) || (notificationStatus === 'accepted' && receivedConfirmation)
  const canSend = canEdit && connection?.status === 'connected' && scopeVerified && hasMailbox && !tooManyRecipients && !dirty && !working && !hasUnresolvedTest && canStartTest
  const receiptEligible = Boolean(notificationStatus === 'accepted' && notificationJob?.status.toLowerCase() === 'complete' && notificationJob.result?.status === 'accepted' && !receivedConfirmation)

  function update<K extends keyof GraphForm>(key: K, value: GraphForm[K]) {
    setForm((current) => ({ ...current, [key]: value }))
    setError(null)
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!canEdit || working) return
    if (tooManyRecipients) {
      setError('Microsoft Graph supports up to 10 configured recipients. Remove the extra addresses before saving.')
      return
    }
    setWorking('save')
    setError(null)
    setAuthenticationMessage(null)
    try {
      const credentials: Record<string, string> = {}
      if (form.tenant_id.trim()) credentials.tenant_id = form.tenant_id.trim()
      if (form.client_id.trim()) credentials.client_id = form.client_id.trim()
      if (form.client_secret.trim()) credentials.client_secret = form.client_secret.trim()
      const settings = {
        sender: form.sender.trim(),
        recipients: parsedRecipients(form.recipients),
        digest_enabled: form.digest_enabled,
      }
      await connectionsApi.save(siteId, 'microsoft_graph', { credentials, settings })
      const savedForm = { ...form, client_secret: '' }
      setForm(savedForm)
      setBaseline(formKey(savedForm))
      setScopeReview(null)
      setScopeInvalidated(true)
      setScopeMailbox(false)
      setScopeNoUnscoped(false)
      setScopeEvidence('')
      await onChanged('Microsoft Graph settings saved. Any previous mailbox scope attestation is invalidated; review the saved configuration again before sending.')
    } catch (requestError) {
      setError(detailMessage(requestError))
    } finally {
      setWorking(null)
    }
  }

  async function testAuthentication() {
    if (!canEdit || !connection || dirty || working) return
    setWorking('authentication')
    setError(null)
    setAuthenticationMessage(null)
    try {
      const job = await connectionsApi.test(siteId, 'microsoft_graph')
      const finished = await jobsApi.wait(siteId, job.id, { onUpdate: (next) => setAuthenticationMessage(`Authentication check is ${next.status}. No email was sent.`) })
      setAuthenticationMessage(`Authentication check finished with status ${finished.status}. This checks Graph access only; no email was sent or delivery confirmed.`)
      await onChanged(`Microsoft Graph authentication check finished with status ${finished.status}. No email was sent.`)
    } catch (requestError) {
      setError(detailMessage(requestError))
    } finally {
      setWorking(null)
    }
  }

  async function reviewScope() {
    if (!canEdit || connection?.status !== 'connected' || dirty || working || !scopeMailbox || !scopeNoUnscoped || scopeEvidence.trim().length < 30 || scopeEvidence.trim().length > 4000) return
    setWorking('scope')
    setError(null)
    try {
      const reviewed = await connectionsApi.reviewMicrosoftGraphScope(siteId, {
        confirms_mailbox_scoped: true,
        confirms_no_unscoped_send: true,
        evidence: scopeEvidence.trim(),
      })
      const nextReview = scopeReviewFrom(reviewed)
      if (!nextReview) throw new Error('The API did not return the recorded owner scope attestation. Refresh the connection before sending.')
      setScopeReview(nextReview)
      setScopeInvalidated(false)
      await onChanged('Owner mailbox scope attestation recorded. This is an owner attestation, not machine verification.')
    } catch (requestError) {
      setError(detailMessage(requestError))
    } finally {
      setWorking(null)
    }
  }

  async function pollNotificationJob(job: Job) {
    setNotificationJob(job)
    setNotificationStatus(notificationTestStatus(job))
    try {
      const finished = await jobsApi.wait(siteId, job.id, {
        timeoutMs: 15_000,
        intervalMs: 500,
        onUpdate: (next) => {
          setNotificationJob(next)
          setNotificationStatus(notificationTestStatus(next))
        },
      })
      setNotificationJob(finished)
      setNotificationStatus(notificationTestStatus(finished))
    } catch (requestError) {
      setNotificationStatus('outcome_unknown')
      setError(`The test job status could not be checked: ${detailMessage(requestError)}`)
    }
  }

  async function submitTestRequest(idempotencyKey: string) {
    setWorking('send')
    setError(null)
    setNotificationStatus('submitting')
    setReceivedConfirmation(false)
    setReceiptConfirmed(false)
    setReceiptNotes('')
    try {
      const job = await notificationsApi.test(siteId, {
        kind: 'microsoft_graph',
        idempotency_key: idempotencyKey,
        confirm_send: true,
      })
      setNotificationJob(job)
      await pollNotificationJob(job)
    } catch (requestError) {
      setNotificationStatus('outcome_unknown')
      setError(`The request outcome is unknown: ${detailMessage(requestError)} Retry only with the same idempotency key.`)
    } finally {
      setWorking(null)
    }
  }

  async function sendOneTest() {
    if (!canSend || working) return
    if (typeof crypto.randomUUID !== 'function') {
      setError('A secure UUID generator is unavailable in this browser.')
      return
    }
    const idempotencyKey = crypto.randomUUID()
    setSendIdempotencyKey(idempotencyKey)
    setNotificationJob(null)
    await submitTestRequest(idempotencyKey)
  }

  async function retryUnknownRequest() {
    if (notificationStatus !== 'outcome_unknown' || notificationJob || !sendIdempotencyKey || working) return
    await submitTestRequest(sendIdempotencyKey)
  }

  async function checkTestStatus() {
    if (!notificationJob || !['queued', 'running', 'outcome_unknown'].includes(notificationStatus) || working) return
    setWorking('send')
    setError(null)
    await pollNotificationJob(notificationJob)
    setWorking(null)
  }

  async function confirmReceipt() {
    if (!canEdit || !notificationJob || !receiptEligible || !receiptConfirmed || receiptNotes.trim().length < 10 || receiptNotes.trim().length > 1000 || working) return
    setWorking('receipt')
    setError(null)
    try {
      const result = await notificationsApi.recordReceipt(siteId, notificationJob.id, {
        confirms_received: true,
        notes: receiptNotes.trim(),
      })
      const receipt = record(result.result?.receipt)
      if (result.result?.recipient_confirmation !== 'confirmed' || receipt.kind !== 'owner_confirmed_recipient_receipt') {
        throw new Error('The API did not confirm the owner recipient receipt. Check the receipt record before treating it as recorded.')
      }
      setReceivedConfirmation(true)
    } catch (requestError) {
      setError(detailMessage(requestError))
    } finally {
      setWorking(null)
    }
  }

  const reviewValid = Boolean(scopeMailbox && scopeNoUnscoped && scopeEvidence.trim().length >= 30 && scopeEvidence.trim().length <= 4000)

  return <section className="connection-card" aria-label="Microsoft 365 Graph connection">
    <div className="connection-card-header">
      <div>
        <div className="connection-card-name">Microsoft 365 Graph</div>
        <div className="connection-card-description">App authentication and mailbox delivery have separate checks. A connection test never sends an email.</div>
      </div>
      <span className="text-small text-muted">{connection?.status ?? 'not configured'}</span>
    </div>

    {!canEdit ? <>
      {currentScopeReview && !scopeReviewExpired && <Notice kind="info" title="Owner attestation recorded">Mailbox scope was reviewed by an owner on {formatDateTime(currentScopeReview.reviewed_at, 'an unknown date')} and expires {reviewExpiry ? formatDateTime(reviewExpiry.toISOString()) : 'after 7 days'}. This is not machine verification.</Notice>}
      {currentScopeReview && scopeReviewExpired && <Notice kind="warning" title="Owner attestation expired">The seven-day mailbox scope attestation expired. An owner must review the connected configuration again.</Notice>}
      {!currentScopeReview && <Notice kind="warning" title="Mailbox scope needs owner review">Only an owner can configure Graph, attest to mailbox scope, send one test message, or record receipt.</Notice>}
      <Notice kind="info">Authentication status does not establish message delivery. ForgeSEO does not read the mailbox.</Notice>
    </> : <form className="stack-sm" onSubmit={(event) => void save(event)}>
      <div className="connection-fields">
        <Field label="Tenant ID"><input aria-label="Tenant ID" value={form.tenant_id} onChange={(event) => update('tenant_id', event.target.value)} autoComplete="off" /></Field>
        <Field label="Client ID"><input aria-label="Client ID" value={form.client_id} onChange={(event) => update('client_id', event.target.value)} autoComplete="off" /></Field>
        <Field label="Client secret" hint="Stored securely. Leave blank to keep the existing secret; saved secrets are never read back."><input aria-label="Client secret" type="password" value={form.client_secret} onChange={(event) => update('client_secret', event.target.value)} autoComplete="new-password" /></Field>
        <Field label="Sender mailbox"><input aria-label="Sender mailbox" type="email" value={form.sender} onChange={(event) => update('sender', event.target.value)} placeholder="reports@example.com" /></Field>
          <Field label="Recipients" hint="Comma or line separated; up to 10 addresses. The one-message test is sent only to these configured recipients." error={tooManyRecipients ? 'Remove recipients until no more than 10 addresses remain.' : undefined}><textarea aria-label="Recipients" value={form.recipients} onChange={(event) => update('recipients', event.target.value)} rows={2} /></Field>
        <label className="checkbox-field"><input aria-label="Enable weekly email digest" type="checkbox" checked={form.digest_enabled} onChange={(event) => update('digest_enabled', event.target.checked)} /><span>Enable weekly email digest</span></label>
      </div>

      {error && <Notice kind="error" title="Microsoft Graph action failed">{error}</Notice>}
      {authenticationMessage && <Notice kind="info" title="Authentication check">{authenticationMessage}</Notice>}
      {scopeInvalidated && <Notice kind="warning" title="Owner attestation needs renewal">Saving Microsoft Graph settings invalidated the previous mailbox scope review.</Notice>}
      {currentScopeReview && !scopeReviewExpired
        ? <Notice kind="success" title="OWNER ATTESTATION">Mailbox scope was attested by owner {currentScopeReview.reviewer_id} on {formatDateTime(currentScopeReview.reviewed_at, 'an unknown date')}. Expires {reviewExpiry ? formatDateTime(reviewExpiry.toISOString()) : 'after 7 days'}. This is not machine verified.</Notice>
        : currentScopeReview && scopeReviewExpired
          ? <Notice kind="warning" title="OWNER ATTESTATION EXPIRED">The seven-day mailbox scope review expired. Record a new owner attestation before sending.</Notice>
        : <Notice kind="warning" title="Mailbox scope has not been owner attested">Review the saved mailbox scope before requesting any test email.</Notice>}

      {connection?.status !== 'connected' && <Notice kind="warning" title="Connect Graph before scope review">Run the authentication test and wait for connected status before recording an owner attestation.</Notice>}
      <fieldset disabled={working !== null || dirty || connection?.status !== 'connected'} style={{ border: 0, padding: 0, margin: 0 }}>
        <div className="stack-sm">
          <label className="checkbox-field"><input type="checkbox" aria-label="Confirm mailbox scoped" checked={scopeMailbox} onChange={(event) => setScopeMailbox(event.target.checked)} /><span>I reviewed Exchange RBAC and confirm Graph mail access is limited to the configured mailbox scope.</span></label>
          <label className="checkbox-field"><input type="checkbox" aria-label="Confirm no unscoped send" checked={scopeNoUnscoped} onChange={(event) => setScopeNoUnscoped(event.target.checked)} /><span>I confirm there is no unscoped or tenant-wide send path for this connection.</span></label>
          <Field label="Admin evidence notes" hint="Record configuration review evidence only; do not paste credentials or tokens. 30–4,000 characters."><textarea aria-label="Admin evidence notes" value={scopeEvidence} onChange={(event) => setScopeEvidence(event.target.value.slice(0, 4000))} minLength={30} maxLength={4000} rows={3} /></Field>
          <Button type="button" variant="secondary" onClick={() => void reviewScope()} disabled={connection?.status !== 'connected' || dirty || working !== null || !reviewValid}>
            <ShieldCheck size={14} /> {working === 'scope' ? 'Recording owner attestation…' : 'Record owner mailbox scope attestation'}
          </Button>
        </div>
      </fieldset>

      <div className="connection-actions">
        <Button type="button" variant="secondary" onClick={() => void testAuthentication()} disabled={!connection || dirty || working !== null}>
          <PlugZap size={14} /> {working === 'authentication' ? 'Testing authentication…' : 'Test authentication (no email)'}
        </Button>
        <Button type="submit" disabled={working !== null}>
          <Save size={14} /> {working === 'save' ? 'Saving…' : 'Save Graph settings'}
        </Button>
      </div>

      <Notice kind="info" title="Authentication is not delivery">The authentication test checks Graph access only. It does not send an email or confirm delivery.</Notice>
      <div className="stack-sm" style={{ borderTop: '1px solid var(--line)', paddingTop: 14 }}>
        <p className="text-small text-muted" style={{ margin: 0 }}>The one-message action requires a connected status, saved sender and recipient settings, and the owner attestation above.</p>
        <Button type="button" onClick={() => void sendOneTest()} disabled={!canSend}>
          <Send size={14} /> {working === 'send' ? 'Requesting one test email…' : notificationStatus === 'failed' || notificationStatus === 'blocked' || receivedConfirmation ? 'Send ONE new test email' : 'Send ONE test email now'}
        </Button>
      </div>

      {(notificationJob || notificationStatus === 'outcome_unknown') && <div className="stack-sm" role="region" aria-label="Microsoft Graph test email job">
        {(() => {
          const outcome = testStatusMessage(notificationStatus)
          return outcome && <Notice kind={outcome.kind} title={outcome.title}>{outcome.body}</Notice>
        })()}
        {(notificationStatus === 'queued' || notificationStatus === 'running' || (notificationStatus === 'outcome_unknown' && notificationJob)) && <Button type="button" variant="secondary" onClick={() => void checkTestStatus()} disabled={working !== null}>{working === 'send' ? 'Checking test job…' : 'Check test job status'}</Button>}
        {notificationStatus === 'outcome_unknown' && !notificationJob && sendIdempotencyKey && <Button type="button" variant="secondary" onClick={() => void retryUnknownRequest()} disabled={working !== null}>{working === 'send' ? 'Retrying same request…' : 'Retry same idempotent request'}</Button>}
        {notificationJob && notificationStatus === 'accepted' && !receivedConfirmation && <>
          <label className="checkbox-field"><input type="checkbox" aria-label="Confirm test email received" checked={receiptConfirmed} onChange={(event) => setReceiptConfirmed(event.target.checked)} disabled={!receiptEligible || working !== null} /><span>I am an intended recipient and confirm the test email arrived in the configured mailbox.</span></label>
          <Field label="Receipt notes" hint="10–1,000 characters."><textarea aria-label="Receipt notes" value={receiptNotes} onChange={(event) => setReceiptNotes(event.target.value.slice(0, 1000))} minLength={10} maxLength={1000} rows={2} disabled={!receiptEligible || working !== null} /></Field>
          <Button type="button" variant="secondary" onClick={() => void confirmReceipt()} disabled={!receiptEligible || !receiptConfirmed || receiptNotes.trim().length < 10 || working !== null}>
            {working === 'receipt' ? 'Recording receipt…' : 'Record owner receipt confirmation'}
          </Button>
        </>}
        {receivedConfirmation && <Notice kind="success" title="Owner receipt confirmation recorded">The API recorded recipient_confirmation confirmed with an owner_confirmed_recipient_receipt. This is an explicit recipient confirmation; ForgeSEO did not read the mailbox.</Notice>}
        {(notificationStatus === 'failed' || notificationStatus === 'blocked') && <p className="text-small text-muted" style={{ margin: 0 }}>Review the job result before requesting a new one-message test.</p>}
      </div>}
    </form>}
  </section>
}
