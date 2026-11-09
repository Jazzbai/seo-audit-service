"""Read-only SEO outcome health, deliberately separate from worker heartbeat."""
import re

from sqlalchemy import select

from app.models import Connection, Job


def automation_health(db, site):
    issues = []
    for connection in db.scalars(select(Connection).where(
            Connection.site_id == site.id, Connection.kind.in_(['gsc', 'ga4']), Connection.status == 'error')):
        issues.append({'code': 'google_connection_failed', 'connection_kind': connection.kind,
                       'message': 'Google reporting needs attention. Reconnect and verify access in Connections.',
                       'destination': 'settings/connections'})
    latest = db.scalar(select(Job).where(Job.site_id == site.id, Job.kind == 'content_autopilot')
                       .order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
    if not site.paused and latest and latest.status in {'partial', 'blocked', 'failed', 'needs_reconciliation'}:
        result = latest.result if isinstance(latest.result, dict) else {}
        codes = result.get('blockers') if isinstance(result.get('blockers'), list) else []
        safe_codes = [value for value in codes if isinstance(value, str) and re.fullmatch(r'[a-z0-9_]{1,64}', value)]
        issues.append({'code': 'content_autopilot_needs_attention', 'job_id': latest.id,
                       'message': 'The latest content autopilot did not confirm publication. Review its research and editorial checks.',
                       'blockers': safe_codes[:8], 'destination': 'jobs'})
    ai = db.scalar(select(Job).where(Job.site_id == site.id, Job.kind == 'visibility',
                                    Job.payload['kind'].as_string() == 'ai_sample')
                   .order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
    if ai and ai.status in {'blocked', 'failed', 'partial'}:
        issues.append({'code': 'ai_measurement_blocked', 'job_id': ai.id,
                       'message': 'The latest AI visibility measurement did not complete. Review the job before relying on older samples.',
                       'destination': 'jobs'})
    return {'status': 'needs_attention' if issues else 'paused' if site.paused else 'no_recorded_blockers',
            'issues': issues, 'scope': 'latest_outcomes_not_scheduler_health'}
