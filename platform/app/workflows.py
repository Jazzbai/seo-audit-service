"""Persistent platform workflows. Models propose; policies authorize writes."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from pathlib import Path

from sqlalchemy import func, select

from app.budgets import release, reserve, settle
from app.config import settings
from app.models import (Article, Candidate, Connection, Finding, Incident, Job,
                        Measurement, Page, Publication, Revision, Site)
from app.network import fetch
from app.operations import credentials, enqueue, event, find_connection, global_controls, iso, now, record
from app.policies import current_policy, evaluate_policy
from app.connectors.errors import AmbiguousOutcome, ConnectorError, ResourceNotFound, SourceConflict


WOO_METADATA_REVIEW_REASON = 'woocommerce_seo_metadata_write_unsupported'
_METADATA_READINESS_REASONS = {
    WOO_METADATA_REVIEW_REASON,
    'woocommerce_connection_required',
    'woocommerce_seo_writer_not_verified',
    'wordpress_connection_required',
    'wordpress_seo_writer_not_verified',
}

# Repeated observations may reuse an active decision or a durable terminal
# record that still represents the current remote value.  A rolled-back or
# stale proposal is different: it is historical evidence, not the current
# action.  Reusing either one would hide a recurring issue when the source
# later returns to the same hash.
_CANDIDATE_REUSE_STATUSES = frozenset({
    'pending', 'approved', 'applied', 'rejected', 'failed',
})


PROGRESS_EVENT_KIND = 'job_progress'
_PROGRESS_STATUSES = frozenset({
    'queued', 'running', 'complete', 'partial', 'failed', 'blocked',
    'retry', 'needs_connection',
})
_PROGRESS_TEXT_LIMITS = {
    'site_id': 64,
    'job_id': 64,
    'job_kind': 64,
    'status': 32,
    'phase': 64,
    'stage': 64,
    'message': 240,
}


def _bounded_progress_text(value, field):
    value = str(value) if value is not None else ''
    return value[:_PROGRESS_TEXT_LIMITS[field]]


def _bounded_progress_int(value, *, minimum, maximum):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(minimum, min(maximum, value))


def emit_job_progress(
    db,
    site,
    *,
    job_id,
    job_kind,
    status,
    phase,
    message,
    stage=None,
    stage_index=None,
    stage_count=None,
    percent=None,
):
    """Persist a bounded, provider-independent progress event."""

    safe_status = status if status in _PROGRESS_STATUSES else 'unknown'
    data = {
        'site_id': _bounded_progress_text(site.id, 'site_id'),
        'job_id': _bounded_progress_text(job_id, 'job_id'),
        'job_kind': _bounded_progress_text(job_kind, 'job_kind'),
        'status': safe_status,
        'phase': _bounded_progress_text(phase, 'phase'),
        'message': _bounded_progress_text(message, 'message'),
    }
    if stage is not None:
        data['stage'] = _bounded_progress_text(stage, 'stage')
    safe_stage_count = _bounded_progress_int(stage_count, minimum=1, maximum=100)
    safe_stage_index = _bounded_progress_int(stage_index, minimum=1, maximum=100)
    if safe_stage_count is not None:
        data['stage_count'] = safe_stage_count
    if safe_stage_index is not None:
        data['stage_index'] = (
            min(safe_stage_index, safe_stage_count)
            if safe_stage_count is not None else safe_stage_index
        )
    safe_percent = _bounded_progress_int(percent, minimum=0, maximum=100)
    if safe_percent is not None:
        data['percent'] = safe_percent
    event(db, site, PROGRESS_EVENT_KIND, data['message'], data)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def protected_html(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html,'html.parser')
    body = soup.body or soup
    return {'body_text':digest(body.get_text(' ',strip=True)),
            'layout':digest([(tag.name,tag.get('class'),tag.get('style'),tag.get('id')) for tag in body.find_all(True)]),
            'styles':digest([str(tag) for tag in soup.select('style,link[rel="stylesheet"]')]),
            'headings':digest([tag.get_text(' ',strip=True) for tag in soup.select('h1,h2,h3')]),
            'canonical':digest([tag.get('href') for tag in soup.select('link[rel="canonical"]')]),
            'robots':digest([tag.get('content') for tag in soup.select('meta[name="robots"]')])}


def candidate_value(source, field):
    key = 'title' if field == 'seo_title' else 'description'
    seo = (((source or {}).get('metadata') or {}).get('seo') or {})
    if not isinstance(seo, dict):
        return ''
    for provider in ('forgeseo', 'yoast', 'rank_math'):
        values = seo.get(provider)
        if isinstance(values, dict) and values.get(key):
            return str(values[key])
    return ''


def metadata_candidate_readiness(db, site, page, field):
    """Return an explicit readiness explanation for a metadata candidate.

    WooCommerce's catalog REST API is intentionally limited to commerce-safe
    editorial fields. Product SEO metadata may be written only through the
    separately verified ForgeSEO connector route; category metadata remains
    review-only until taxonomy rendering and write semantics are verified.
    WordPress metadata also remains pending until its capability check proves
    the narrow writer.
    """

    if page.resource_type in {'products', 'product'} and field in {
        'seo_title',
        'meta_description',
    }:
        connection = find_connection(db, site.id, 'woocommerce')
        capabilities = connection.capabilities if connection is not None and isinstance(connection.capabilities, dict) else {}
        seo = capabilities.get('seo') if isinstance(capabilities.get('seo'), dict) else {}
        writable_fields = seo.get('writable_fields') if isinstance(seo.get('writable_fields'), list) else []
        resource_types = seo.get('resource_types') if isinstance(seo.get('resource_types'), list) else []
        provider_field = 'title' if field == 'seo_title' else 'description'
        if connection is None or connection.status == 'revoked':
            return {
                'execution_capable': False,
                'execution_ready': False,
                'blockers': ['woocommerce_connection_required'],
            }
        if (
            seo.get('write') is not True
            or provider_field not in writable_fields
            or 'product' not in resource_types
        ):
            return {
                'execution_capable': False,
                'execution_ready': False,
                'blockers': ['woocommerce_seo_writer_not_verified'],
            }
    if page.resource_type in {'categories', 'product_categories', 'category'} and field in {
        'seo_title',
        'meta_description',
    }:
        connection = find_connection(db, site.id, 'woocommerce')
        capabilities = connection.capabilities if connection is not None and isinstance(connection.capabilities, dict) else {}
        seo = capabilities.get('seo') if isinstance(capabilities.get('seo'), dict) else {}
        writable_fields = seo.get('writable_fields') if isinstance(seo.get('writable_fields'), list) else []
        resource_types = seo.get('resource_types') if isinstance(seo.get('resource_types'), list) else []
        provider_field = 'title' if field == 'seo_title' else 'description'
        if connection is None or connection.status == 'revoked':
            return {
                'execution_capable': False,
                'execution_ready': False,
                'blockers': ['woocommerce_connection_required'],
            }
        if (
            seo.get('write') is True
            and provider_field in writable_fields
            and 'category' in resource_types
        ):
            return None
        return {
            'execution_capable': False,
            'execution_ready': False,
            'blockers': ['woocommerce_seo_writer_not_verified'],
        }
    if page.resource_type in {'posts', 'pages'} and field in {'seo_title', 'meta_description'}:
        connection = find_connection(db, site.id, 'wordpress')
        capabilities = connection.capabilities if connection is not None and isinstance(connection.capabilities, dict) else {}
        seo = capabilities.get('seo') if isinstance(capabilities.get('seo'), dict) else {}
        writable_fields = seo.get('writable_fields') if isinstance(seo.get('writable_fields'), list) else []
        provider_field = 'title' if field == 'seo_title' else 'description'
        if connection is None or connection.status == 'revoked':
            return {
                'execution_capable': False,
                'execution_ready': False,
                'blockers': ['wordpress_connection_required'],
            }
        if seo.get('write') is not True or provider_field not in writable_fields:
            return {
                'execution_capable': False,
                'execution_ready': False,
                'blockers': ['wordpress_seo_writer_not_verified'],
            }
    return None


def review_only_reasons(candidate):
    details = candidate.details if isinstance(candidate.details, dict) else {}
    reasons = details.get('review_only_reasons', [])
    if not isinstance(reasons, list):
        return []
    return [str(reason) for reason in reasons if str(reason).strip()]


def apply_metadata_readiness(details, readiness):
    """Refresh connector readiness without erasing other review evidence."""

    result = dict(details or {})
    existing = result.get('review_only_reasons', [])
    if not isinstance(existing, list):
        existing = []
    retained = [
        str(reason)
        for reason in existing
        if str(reason).strip() and str(reason) not in _METADATA_READINESS_REASONS
    ]
    if readiness is not None:
        result['review_only_reasons'] = list(dict.fromkeys([
            *retained,
            *readiness['blockers'],
        ]))
        result['execution_readiness'] = readiness
    elif retained:
        result['review_only_reasons'] = retained
        result['execution_readiness'] = {
            'execution_capable': True,
            'execution_ready': True,
            'blockers': [],
        }
    else:
        result.pop('review_only_reasons', None)
        if any(
            isinstance(result.get('execution_readiness'), dict)
            and reason in result['execution_readiness'].get('blockers', [])
            for reason in _METADATA_READINESS_REASONS
        ):
            result['execution_readiness'] = {
                'execution_capable': True,
                'execution_ready': True,
                'blockers': [],
            }
    return result


def candidate_execution_blockers(db, site, row, page=None):
    page = page or db.get(Page, row.page_id)
    reasons = review_only_reasons(row)
    if page is not None:
        readiness = metadata_candidate_readiness(db, site, page, row.field)
        if readiness is not None:
            reasons.extend(readiness['blockers'])
    return list(dict.fromkeys(reasons))


def capture_html(site,job,url,html,phase='audit'):
    body=html.encode('utf-8')
    checksum=hashlib.sha256(body).hexdigest()
    root=Path(settings.ARTIFACT_ROOT)
    path=root/site.id/'evidence'/f'{checksum}.html'
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        path.write_bytes(body)
    return {'artifact':path.relative_to(root).as_posix(),'sha256':checksum,'bytes':len(body),
            'url':url,'observed_at':iso(now()),'job_id':job.id,'phase':phase,'observation_type':'source_html'}


def scoped(db, cls, row_id, site):
    row = db.scalar(select(cls).where(cls.id == row_id, cls.site_id == site.id))
    if row is None:
        raise ValueError('The selected resource does not belong to this site')
    return row


def authorize(db, site, action, page=None):
    db.refresh(site)
    policy = current_policy(db, site.id)
    blockers = evaluate_policy(site, policy, action, page, global_pause=global_controls(db)['global_pause'])
    if blockers:
        raise ValueError('Action paused: ' + ', '.join(blockers))
    return policy


def incident(db, site, key, title, kind='workflow', severity='high', details=None):
    row = db.scalar(select(Incident).where(Incident.site_id == site.id, Incident.key == key))
    if row is None:
        row = Incident(site_id=site.id, key=key, kind=kind, severity=severity, title=title,
                       first_seen_at=now(), last_seen_at=now(), details=details or {}, status='open')
        db.add(row)
    else:
        row.status, row.last_seen_at, row.resolved_at = 'open', now(), None
        row.details = details or {}
    return row


def canonical_resource_identity(source):
    """Return the same resource type/key shape used by stored pages.

    Connectors use singular resource keys (for example ``post:12``) while
    the platform stores collection-oriented page types (``posts:12``).
    Inventory reconciliation must canonicalize both sides or a healthy
    resource is incorrectly reported as missing on every run.
    """

    raw_key = str(source.get('resource_key') or '')
    kind = str(source.get('resource_type') or (raw_key.split(':', 1)[0] if ':' in raw_key else 'unknown'))
    normalized = {
        'post': 'posts',
        'posts': 'posts',
        'page': 'pages',
        'pages': 'pages',
        'product': 'products',
        'products': 'products',
        'category': 'product_categories',
        'categories': 'product_categories',
        'product_category': 'product_categories',
        'product_categories': 'product_categories',
        'author': 'authors',
        'authors': 'authors',
    }.get(kind, kind)
    key = f"{normalized}:{raw_key.split(':', 1)[1]}" if ':' in raw_key else raw_key
    return normalized, key


def store_page(db, site, source):
    normalized, key = canonical_resource_identity(source)
    page = db.scalar(select(Page).where(Page.site_id == site.id, Page.resource_key == key))
    if page is None:
        page = Page(site_id=site.id, resource_key=key)
        db.add(page)
    page.url = source.get('url') or source.get('public_url') or site.origin
    title = source.get('title', '')
    page.title = title.get('rendered', '') if isinstance(title, dict) else str(title)
    page.resource_type = normalized
    page.source = source
    page.source_hash = source.get('source_hash') or digest(source)
    page.last_seen_at = now()
    # Platform-created articles are eligible for the maintenance workflow by
    # default. Existing WordPress content remains unenrolled until an owner
    # explicitly opts it in. The remote ID is the durable link between a
    # verified platform publication and the later inventory observation.
    if normalized in {'posts', 'pages'}:
        remote_id = key.split(':', 1)[1] if ':' in key else ''
        managed_article = db.scalar(select(Article).where(
            Article.site_id == site.id,
            Article.remote_id == remote_id,
            Article.managed.is_(True),
        )) if remote_id else None
        if managed_article is not None:
            page.managed = True
            page.enrolled = True
            page.signals = {
                **(page.signals or {}),
                'enrollment': {
                    'mode': 'platform_created',
                    'article_id': managed_article.id,
                    'updated_at': iso(now()),
                },
            }
    db.flush()
    return page


async def client_for(db, site, kind='wordpress'):
    from app.connectors.wordpress import WordPressClient
    from app.connectors.woocommerce import WooCommerceClient
    secret, _ = credentials(db, site.id, kind)
    return (WooCommerceClient if kind == 'woocommerce' else WordPressClient)(site.origin, secret)


async def inventory(db, site, job):
    counts = {}
    seen_by_kind = {}
    type_by_kind = {'wordpress': {'posts','pages','post','page','media','authors','author'},
                    'woocommerce': {'products','product_categories','product','category','categories'}}
    for kind in ['wordpress', 'woocommerce']:
        connection = find_connection(db, site.id, kind)
        if connection is None or connection.status == 'revoked':
            if kind == 'wordpress':
                raise ValueError('Connect WordPress before importing inventory')
            continue
        async with await client_for(db, site, kind) as client:
            # Discovery describes the public API surface; validate_connection
            # additionally verifies authenticated access and permissions before
            # those capabilities are persisted for the UI and later writes.
            capabilities = await client.validate_connection()
            sources = await client.inventory()
        connection.capabilities = {**connection.capabilities, **capabilities}
        connection.status = 'connected'
        connection.checked_at = now()
        for source in sources:
            store_page(db, site, source)
        seen_by_kind[kind] = {
            canonical_resource_identity(source)[1]
            for source in sources
            if source.get('resource_key')
        }
        counts[kind] = len(sources)
        missing = 0
        for page in db.scalars(select(Page).where(Page.site_id == site.id,
                                                   Page.resource_type.in_(type_by_kind[kind]))):
            status = 'present' if page.resource_key in seen_by_kind[kind] else 'missing'
            if status == 'missing':
                missing += 1
            page.signals = {**(page.signals or {}), 'inventory': {
                'status': status,
                'checked_at': iso(now()),
                'source': kind,
            }}
        counts[kind] = {'seen': len(sources), 'missing': missing}
        db.commit()
    event(db, site, 'inventory_complete', 'Connected inventory synchronized', counts)
    return {'resources': sum(item['seen'] for item in counts.values()), 'counts': counts, 'complete': True}


def _poll_datetime(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.rstrip('Z'))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


async def poll_changes(db, site, job):
    """Poll WordPress changes and queue read-only targeted audits.

    The cursor is advanced only after the remote read succeeds. The next
    window overlaps the last successful cursor so a delayed scheduler tick or
    boundary change is seen at least once; source-hash idempotency keys collapse
    duplicates caused by that overlap.
    """
    connection = find_connection(db, site.id, 'wordpress')
    if connection is None or connection.status == 'revoked':
        raise ValueError('Connect WordPress before polling changes')

    capabilities = dict(connection.capabilities or {})
    state = capabilities.get('change_poll')
    state = dict(state) if isinstance(state, dict) else {}
    previous_cursor = _poll_datetime(state.get('cursor') or state.get('last_success_at'))
    poll_started = now()
    cursor = previous_cursor - timedelta(minutes=5) if previous_cursor else poll_started - timedelta(minutes=10)
    cursor_marker = iso(cursor)

    async with await client_for(db, site, 'wordpress') as client:
        sources = await client.inventory(modified_after=cursor_marker)

    scheduled = []
    for source in sources:
        page = store_page(db, site, source)
        # Authors and media are not independently auditable page resources.
        # The connector omits authors during incremental polling; this guard
        # keeps custom connector implementations conservative as well.
        if page.resource_type in {'authors', 'author', 'media', 'attachments'}:
            continue
        targeted = enqueue(
            db,
            site,
            'targeted_audit',
            {'resource_key': page.resource_key},
            f'change-poll:{page.resource_key}:{page.source_hash}',
        )
        scheduled.append(targeted.id)

    completed_at = now()
    state.update({
        'cursor': iso(completed_at),
        'last_success_at': iso(completed_at),
        'last_result_at': iso(completed_at),
        'last_seen_count': len(sources),
        'last_scheduled_count': len(scheduled),
        'last_cursor': cursor_marker,
    })
    capabilities['change_poll'] = state
    connection.capabilities = capabilities
    connection.checked_at = completed_at
    event(
        db,
        site,
        'change_poll_complete',
        'WordPress changes polled',
        {
            'cursor': cursor_marker,
            'seen': len(sources),
            'targeted_audits': len(scheduled),
            'completed_at': iso(completed_at),
        },
    )
    db.commit()
    return {
        'complete': True,
        'cursor': cursor_marker,
        'seen': len(sources),
        'targeted_audit_job_ids': scheduled,
    }


def _lock_observation_page(db, site, page):
    """Serialize observations for one page across worker transactions.

    Candidate identity is intentionally not globally unique: a rolled-back
    candidate must be able to reappear as a fresh actionable record later.
    That means the active-candidate lookup in ``upsert_observation`` cannot be
    protected by a permanent uniqueness constraint alone. Locking the owning
    page for the duration of the observation gives PostgreSQL workers one
    durable serialization point while preserving historical candidate rows.
    ``with_for_update`` is a no-op on the isolated SQLite unit-test database;
    production PostgreSQL is the multi-worker safety boundary.
    """

    if page.id is None:
        db.flush()
    locked = db.scalar(
        select(Page)
        .where(Page.id == page.id, Page.site_id == site.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise ValueError('The observed page does not belong to this site')
    return locked


def upsert_observation(db, site, page, observation, complete=True):
    # Candidate deduplication and sibling rebasing below must see one
    # transactionally ordered observation per page. Reassign to the refreshed
    # row so a worker that waited on another transaction does not use stale
    # source/signals from before it acquired the lock.
    page = _lock_observation_page(db, site, page)
    rendered = observation['signals'].get('observation_type') == 'browser_rendered'
    previous = dict(page.signals or {})
    page.signals = ({**previous, 'browser':observation['signals']} if rendered else
                    {**observation['signals'], **({'browser':previous['browser']} if 'browser' in previous else {})})
    seen = set()
    findings_by_key = {}
    for item in observation.get('findings', []):
        key = f'{page.resource_key}:{item.get("key", item["code"])}'
        if key in findings_by_key:
            # A page can contain many instances of the same issue (for
            # example, several empty links), but findings are intentionally
            # page-level durable records. Keep one record and retain bounded
            # occurrence evidence instead of relying on session autoflush to
            # discover a duplicate that is still pending in this batch.
            details = findings_by_key[key].setdefault('details', {})
            count = int(details.get('occurrence_count', 1))
            details['occurrence_count'] = count + 1
            occurrences = details.setdefault('occurrences', [])
            if isinstance(occurrences, list) and len(occurrences) < 25:
                occurrences.append(item.get('details', {}))
            continue
        findings_by_key[key] = {
            'code': item['code'],
            'severity': item['severity'],
            'title': item['title'],
            'details': dict(item.get('details', {})),
        }

    for key, item in findings_by_key.items():
        seen.add(key)
        row = next(
            (
                pending
                for pending in db.new
                if isinstance(pending, Finding)
                and pending.site_id == site.id
                and pending.key == key
            ),
            None,
        )
        if row is None:
            row = db.scalar(select(Finding).where(Finding.site_id == site.id, Finding.key == key))
        if row is None:
            row = Finding(site_id=site.id, page_id=page.id, key=key, code=item['code'],
                          severity=item['severity'], title=item['title'], details=item.get('details',{}),
                          first_seen_at=now(), last_seen_at=now(), status='open')
            db.add(row)
        else:
            if row not in db.new and row.status == 'resolved':
                row.recurrence_count += 1
            row.status, row.last_seen_at, row.resolved_at = 'open', now(), None
            row.details = item.get('details', {})
    if complete:
        for row in db.scalars(select(Finding).where(Finding.site_id == site.id, Finding.page_id == page.id, Finding.status == 'open')):
            same_observation = row.key.startswith(page.resource_key + ':browser:') == rendered
            if same_observation and row.key not in seen:
                row.status, row.resolved_at = 'resolved', now()
    candidates_by_identity = {}
    for item in observation.get('candidates', []):
        field = item['field']
        before, after = str(item.get('before_value','')), str(item.get('after_value',''))
        if before == after or not after:
            continue
        candidates_by_identity.setdefault((field, before, after), item)

    for item in candidates_by_identity.values():
        field = item['field']
        before, after = str(item.get('before_value','')), str(item.get('after_value',''))
        existing = next(
            (
                pending
                for pending in db.new
                if isinstance(pending, Candidate)
                and pending.site_id == site.id
                and pending.page_id == page.id
                and pending.field == field
                and pending.source_hash == page.source_hash
                and pending.after_value == after
                and pending.status in _CANDIDATE_REUSE_STATUSES
            ),
            None,
        )
        if existing is None:
            existing = db.scalar(select(Candidate).where(Candidate.site_id == site.id, Candidate.page_id == page.id,
                                 Candidate.field == field, Candidate.source_hash == page.source_hash,
                                 Candidate.after_value == after,
                                 Candidate.status.in_(_CANDIDATE_REUSE_STATUSES)))
        if existing:
            readiness = metadata_candidate_readiness(db, site, page, field)
            existing.details = apply_metadata_readiness(existing.details, readiness)
            continue
        # Only evidence for this exact target can stale that target's earlier drafts.
        for old in db.scalars(select(Candidate).where(Candidate.site_id == site.id, Candidate.page_id == page.id,
                              Candidate.field == field, Candidate.status.in_(['pending','approved']))):
            if old.source_hash != page.source_hash:
                old.status = 'stale'
        readiness = metadata_candidate_readiness(db, site, page, field)
        details = apply_metadata_readiness(item.get('details', {}), readiness)
        db.add(Candidate(site_id=site.id, page_id=page.id, field=field,
                         before_value=before, after_value=after, source_hash=page.source_hash,
                         status='pending', details=details))


def _audit_reconciliation_record(item, page=None, signals=None):
    """Keep only bounded, non-HTML crawl evidence for continuation jobs."""

    if not isinstance(item, dict) or not isinstance(item.get('url'), str):
        return None
    record = {
        'url': item['url'],
        'status_code': item.get('status_code'),
        'error': item.get('error'),
    }
    if isinstance(item.get('requested_url'), str):
        record['requested_url'] = item['requested_url']
    if isinstance(item.get('redirect_chain'), list):
        record['redirect_chain'] = [value for value in item['redirect_chain'] if isinstance(value, str)][:8]
    if page is not None:
        record['resource_type'] = page.resource_type
    elif isinstance(item.get('resource_type'), str):
        record['resource_type'] = item['resource_type']
    if isinstance(item.get('page_purpose'), str):
        record['page_purpose'] = item['page_purpose']
    if item.get('browser_observed') is True:
        record['browser_observed'] = True
    if isinstance(signals, dict):
        record['signals'] = {
            'source': signals.get('source'),
            'source_html': signals.get('source_html'),
            'page_purpose': signals.get('page_purpose'),
            'metadata': {
                'canonical': ((signals.get('metadata') or {}).get('canonical')
                              if isinstance(signals.get('metadata'), dict) else None),
            },
        }
        record['source_html_checked'] = signals.get('source') == 'source_html' or bool(
            signals.get('source_html')
        )
    return record


def _bounded_reconciliation_records(value):
    if not isinstance(value, list):
        return []
    records = []
    for item in value[-1000:]:
        record = _audit_reconciliation_record(item)
        if record is not None:
            records.append(record)
    return records


def _upsert_site_reconciliation(db, site, reconciliation, *, complete):
    """Persist site-scope evidence without mixing it into page namespaces."""

    prefix = f'{site.id}:site_reconciliation:'
    seen = set()
    for item in reconciliation.get('findings', []):
        details = item.get('details') if isinstance(item, dict) else None
        details = details if isinstance(details, dict) else {}
        identity = details.get('identity') or details
        code = str(item.get('code') or 'site_reconciliation')
        key = f'{prefix}{code}:{digest(identity)[:40]}'
        seen.add(key)
        row = db.scalar(select(Finding).where(Finding.site_id == site.id, Finding.key == key))
        if row is None:
            row = Finding(
                site_id=site.id,
                page_id=None,
                key=key,
                code=code,
                severity=str(item.get('severity') or 'warning'),
                title=str(item.get('title') or 'Site-wide reconciliation finding'),
                details=details,
                first_seen_at=now(),
                last_seen_at=now(),
                status='open',
            )
            db.add(row)
        else:
            if row.status == 'resolved':
                row.recurrence_count += 1
            row.status = 'open'
            row.last_seen_at = now()
            row.resolved_at = None
            row.severity = str(item.get('severity') or row.severity)
            row.title = str(item.get('title') or row.title)
            row.details = details

    # A partial crawl cannot prove that an old site-scope issue disappeared.
    # Only a complete bounded crawl may resolve reconciliation findings absent
    # from the new evidence set.
    if complete:
        for row in db.scalars(select(Finding).where(Finding.site_id == site.id)).all():
            if row.page_id is None and row.key.startswith(prefix) and row.key not in seen:
                if row.status == 'open':
                    row.status = 'resolved'
                    row.resolved_at = now()


_GOVERNED_METADATA_LIMIT = 10
_GOVERNED_REASON_LIMIT = 8
_GOVERNED_REASON_LENGTH = 80
_SAFE_GOVERNED_REASON_CODES = frozenset({
    'global_pause',
    'site_paused',
    'policy_missing',
    'policy_site_mismatch',
    'policy_invalid',
    'policy_disabled',
    'action_not_allowed',
    'protected_path',
    'page_not_enrolled',
    'candidate_page_missing',
    'verified_writable_resource_required',
    'unsupported_metadata_field',
    'metadata_editorial_checks_failed',
    'owner_review_required',
    'manual_review_required',
    'review_required',
    'metadata_execution_summary_unavailable',
}) | _METADATA_READINESS_REASONS


def _safe_reason_codes(values):
    """Keep governed summaries to stable, non-provider reason codes."""

    if not isinstance(values, (list, tuple, set)):
        values = [values]
    output = []
    for value in values:
        raw = str(value)
        normalized = raw[:_GOVERNED_REASON_LENGTH] if raw in _SAFE_GOVERNED_REASON_CODES else 'review_required'
        if normalized and normalized not in output:
            output.append(normalized)
        if len(output) >= _GOVERNED_REASON_LIMIT:
            break
    return output


def _governed_metadata_execution(db, site):
    """Authorize only bounded metadata candidates for a governed audit.

    This is deliberately a queueing gate, not a connector writer. The
    candidate worker performs the final authorization, source-readiness, and
    source-hash checks immediately before any remote metadata write.
    """

    db.refresh(site)
    policy = current_policy(db, site.id)
    controls = global_controls(db)
    policy_blockers = evaluate_policy(
        site,
        policy,
        'metadata',
        global_pause=bool(controls.get('global_pause')),
    )
    candidates = db.scalars(
        select(Candidate)
        .where(Candidate.site_id == site.id, Candidate.status == 'pending')
        .limit(_GOVERNED_METADATA_LIMIT)
    ).all()
    summary = {
        'candidate_limit': _GOVERNED_METADATA_LIMIT,
        'examined_count': len(candidates),
        'authorized_count': 0,
        'queued_count': 0,
        'authorized_candidate_ids': [],
        'queued_job_ids': [],
        'review_gated': [],
        'gate_blockers': _safe_reason_codes(policy_blockers),
    }

    def review_gate(row, reasons):
        summary['review_gated'].append({
            'candidate_id': row.id,
            'reasons': _safe_reason_codes(reasons),
        })

    if policy_blockers:
        for row in candidates:
            review_gate(row, policy_blockers)
        return summary

    for row in candidates:
        page = db.scalar(
            select(Page).where(Page.id == row.page_id, Page.site_id == site.id)
        )
        blockers = []
        if page is None:
            blockers.append('candidate_page_missing')
        elif page.resource_type == 'discovered_page':
            blockers.append('verified_writable_resource_required')
        else:
            blockers.extend(candidate_execution_blockers(db, site, row, page))
            blockers.extend(evaluate_policy(
                site,
                policy,
                'metadata',
                page,
                global_pause=bool(controls.get('global_pause')),
            ))
            if row.field not in ('seo_title', 'meta_description'):
                blockers.append('unsupported_metadata_field')
            else:
                from app.intelligence.content import check_metadata

                if not check_metadata(row.field, row.after_value).get('passed'):
                    blockers.append('metadata_editorial_checks_failed')
        blockers = list(dict.fromkeys(blockers))
        if blockers:
            review_gate(row, blockers)
            continue

        # A site policy is an explicit authorization class for the action. The
        # candidate worker calls the same policy and readiness checks again
        # after it claims the queued job.
        row.status = 'approved'
        row.policy_version = policy.version
        row.details = {
            **(row.details or {}),
            'authorization': {
                'type': 'policy',
                'action': 'metadata',
                'version': policy.version,
                'at': iso(now()),
            },
        }
        event(
            db,
            site,
            'candidate_policy_authorized',
            f'Metadata candidate authorized by policy v{policy.version}',
            {'candidate_id': row.id, 'policy_version': policy.version},
        )
        queued = enqueue(
            db,
            site,
            'candidate',
            {'candidate_id': row.id},
            f'candidate:{row.id}',
        )
        summary['authorized_candidate_ids'].append(row.id)
        summary['queued_job_ids'].append(queued.id)

    summary['authorized_count'] = len(summary['authorized_candidate_ids'])
    summary['queued_count'] = len(summary['queued_job_ids'])
    return summary


async def audit(db, site, job):
    from app.intelligence.audit import audit_page, crawl, reconcile_site_audit, representative_template_key
    payload = job.payload if isinstance(job.payload, dict) else {}
    continuation = any(key in payload for key in ('seed_urls', 'visited_urls'))
    reconciliation_records = _bounded_reconciliation_records(payload.get('reconciliation_pages'))
    wordpress_connection = find_connection(db, site.id, 'wordpress')
    if (wordpress_connection is not None and wordpress_connection.status != 'revoked'
            and not continuation and not payload.get('skip_inventory')):
        await inventory(db, site, job)
    try:
        batch_size = int(payload.get('max_pages', 25))
    except (TypeError, ValueError):
        batch_size = 25
    batch_size = min(max(batch_size, 1), 100)
    result = await crawl(
        site.origin,
        max_pages=batch_size,
        seed_urls=payload.get('seed_urls') if 'seed_urls' in payload else None,
        visited_urls=payload.get('visited_urls') if 'visited_urls' in payload else None,
    )
    sources = {p.url.rstrip('/'): p for p in db.scalars(select(Page).where(Page.site_id == site.id))}
    checked = 0
    rendered_candidates = []
    for item in result['pages']:
        url = item['url']
        page = sources.get(url.rstrip('/'))
        if page is None:
            page = store_page(db, site, {'resource_key':'url:' + digest(url)[:24], 'resource_type':'discovered_page',
                                       'url':url,'title':'','body':'','source_hash':digest(item.get('html',''))})
        status = item.get('status_code', 0)
        if status != 200 or item.get('error'):
            record = _audit_reconciliation_record(item, page=page)
            if record is not None:
                reconciliation_records.append(record)
            issue = {'key':'page_unavailable','code':'page_unavailable','severity':'high','title':'Page could not be checked',
                     'details':{'status_code':status}}
            upsert_observation(db, site, page, {'signals':{'status_code':status, 'observation_type':'source_html'},'findings':[issue]}, complete=False)
            continue
        observation = audit_page(url, item.get('html',''), page.source)
        evidence=capture_html(site,job,url,item.get('html',''))
        observation['signals'] = {**observation['signals'], 'status_code':status,'observed_at':iso(now()),'observation_type':'source_html','evidence':evidence}
        for finding in observation['findings']:
            finding['details']={**finding.get('details',{}),'evidence':evidence}
        upsert_observation(db, site, page, observation)
        record = _audit_reconciliation_record(item, page=page, signals=observation['signals'])
        if record is not None:
            reconciliation_records.append(record)
        checked += 1
        rendered_candidates.append((page, observation))
    db.commit()
    # A source crawl cannot prove that client-side rendering matches the
    # published page. Queue a small, representative browser sample for every
    # audit batch, plus any page whose previous rendered check had failures.
    # Browser checks are read-only and run on the dedicated browser queue.
    browser_jobs = []
    prioritized = sorted(
        rendered_candidates,
        key=lambda pair: (
            0 if isinstance((pair[0].signals or {}).get('browser'), dict)
            and (pair[0].signals or {}).get('browser', {}).get('resource_failures') else 1,
            0 if pair[0].resource_type in {'posts', 'pages', 'products', 'product_categories'} else 1,
            pair[0].resource_key,
        ),
    )
    seen_templates = set()
    seen_page_ids = set()
    selected = []
    for page, _observation in prioritized:
        if page.id in seen_page_ids:
            continue
        resource_type = page.resource_type or 'unknown'
        page_purpose = ((page.signals or {}).get('page_purpose')
                        if isinstance(page.signals, dict) else None)
        template_key = representative_template_key(
            page.url,
            resource_type=resource_type,
            page_purpose=page_purpose,
        )
        if template_key not in seen_templates:
            selected.append(page)
            seen_templates.add(template_key)
            seen_page_ids.add(page.id)
        if len(selected) >= 3:
            break
    for page in selected:
        browser_job = enqueue(
            db,
            site,
            'browser',
            {'page_id': page.id, 'audit_job_id': job.id, 'reason': 'audit_sample'},
            f'browser:audit:{job.id}:{page.id}',
        )
        browser_jobs.append(browser_job.id)
    pending_urls = result.get('pending_urls', [])
    errors = result.get('errors', [])
    if not isinstance(pending_urls, list):
        pending_urls = []
    if not isinstance(errors, list):
        errors = []
    # A crawl that has no pending queue but did record transport/HTTP errors
    # is not strong enough evidence to resolve a prior site-scope finding.
    reconciliation_complete = bool(result.get('complete', False)) and not pending_urls and not errors
    reconciliation = reconcile_site_audit(
        reconciliation_records,
        site.origin,
        browser_sample_urls=[page.url for page in selected],
    )
    reconciliation['crawl_complete'] = bool(result.get('complete', False))
    reconciliation['resolution_allowed'] = reconciliation_complete
    _upsert_site_reconciliation(db, site, reconciliation, complete=reconciliation_complete)
    continuation_job_id = None
    if not result.get('complete', False):
        retry = payload.get('cursor_retry', 0)
        retry = retry if isinstance(retry, int) and not isinstance(retry, bool) and retry >= 0 else 0
        cursor = {
            'max_pages': batch_size,
            'seed_urls': pending_urls,
            'visited_urls': result.get('visited_urls', []),
            'reconciliation_pages': reconciliation_records[-1000:],
            'cursor_retry': retry + (0 if pending_urls else 1),
        }
        # A crawl with pending URLs is continued immediately. If discovery or
        # transport errors left no pending URL, retry the same cursor only a
        # bounded number of times; an empty queue is not completion evidence.
        can_retry = bool(pending_urls) or retry < 3
        if can_retry:
            continuation_key = 'audit:continuation:' + digest(cursor)
            continuation_job = enqueue(db, site, 'audit', cursor, continuation_key)
            if not pending_urls:
                continuation_job.available_at = now() + timedelta(seconds=min(300, 30 * (2 ** retry)))
                db.commit()
            continuation_job_id = continuation_job.id
        else:
            incident(db, site, 'audit:discovery_incomplete', 'Audit could not complete after bounded retries',
                     kind='audit', severity='high', details={'errors': errors, 'visited_urls': result.get('visited_urls', [])})
    event(db, site, 'audit_complete', f'Audit batch checked {checked} pages',
          {'complete':result['complete'], 'pending_urls':len(pending_urls), 'continuation_job_id':continuation_job_id,
           'errors':errors, 'reconciliation': {
               'pages_considered': reconciliation['pages_considered'],
               'duplicate_url_groups': len(reconciliation['duplicate_urls']),
               'redirects': len(reconciliation['redirects']),
               'redirect_chains': len(reconciliation['redirect_chains']),
               'templates': reconciliation['template_coverage']['template_count'],
           }})
    # Automatic metadata actions are independently authorized again in each
    # worker. A governed full cycle uses the stricter, summarized variant so
    # its result can account for every candidate without reflecting provider
    # payloads. Read-only full cycles retain the historical gate below, which
    # is intentionally disabled by their stage payload.
    metadata_execution = None
    if (
        payload.get('full_cycle_mode') == 'governed'
        and not payload.get('suppress_automation')
    ):
        metadata_execution = _governed_metadata_execution(db, site)
    else:
        policy = current_policy(db, site.id)
        if (not payload.get('suppress_automation') and policy and policy.settings.get('enabled')
                and not site.paused and not global_controls(db)['global_pause']):
            candidates = db.scalars(select(Candidate).where(Candidate.site_id == site.id, Candidate.status == 'pending').limit(10)).all()
            for candidate in candidates:
                # Candidate rows may be imported from an older installation or
                # be corrupted independently of their page foreign key. Resolve
                # the page through the same site boundary before policy can
                # authorize or queue any automatic mutation.
                page = db.scalar(select(Page).where(
                    Page.id == candidate.page_id,
                    Page.site_id == site.id,
                ))
                if (page and page.resource_type != 'discovered_page'
                        and not review_only_reasons(candidate)
                        and not evaluate_policy(site, policy, 'metadata', page)):
                    # A site policy is an explicit authorization class for the
                    # actions the owner enabled. Persist that decision before
                    # the job is queued; the worker still rechecks policy,
                    # source freshness, and public verification immediately
                    # before the remote write. This keeps automatic fixes
                    # auditable while avoiding a permanently pending job that
                    # the worker must correctly refuse.
                    candidate.status = 'approved'
                    candidate.policy_version = policy.version
                    candidate.details = {
                        **(candidate.details or {}),
                        'authorization': {
                            'type': 'policy',
                            'action': 'metadata',
                            'version': policy.version,
                            'at': iso(now()),
                        },
                    }
                    event(db, site, 'candidate_policy_authorized',
                          f'Metadata candidate authorized by policy v{policy.version}',
                          {'candidate_id': candidate.id, 'policy_version': policy.version})
                    enqueue(db, site, 'candidate', {'candidate_id':candidate.id}, f'candidate:{candidate.id}')
    output = {'complete':result['complete'],'checked_pages':checked,'pending_urls':pending_urls,
              'visited_urls':result.get('visited_urls',[]),'continuation_job_id':continuation_job_id,
              'browser_job_ids':browser_jobs,'errors':errors,'reconciliation':reconciliation}
    if metadata_execution is not None:
        output['metadata_execution'] = metadata_execution
    return output


async def availability(db, site, job):
    failed = False
    try:
        response = await fetch(site.origin)
        status = response['status_code']
        failed = status >= 500 or status == 0
    except Exception:
        status, failed = 0, True
    row = db.scalar(select(Incident).where(Incident.site_id == site.id, Incident.key == 'availability'))
    if failed:
        if row is None:
            row = incident(db, site, 'availability', 'Site availability check failed', kind='availability')
        row.failure_count += 1
        row.status = 'open' if row.failure_count >= 2 else 'confirming'
        row.last_seen_at = now()
        row.details = {'status_code':status}
        if row.failure_count == 2:
            event(db, site, 'outage_confirmed', 'Two consecutive availability checks failed')
    elif row:
        if row.status == 'open':
            event(db, site, 'outage_resolved', 'Site availability recovered')
        row.status, row.failure_count, row.resolved_at = 'resolved', 0, now()
    db.add(Measurement(site_id=site.id, kind='availability', source='http_probe', data={'status_code':status}, observed_at=now()))
    return {'available':not failed,'status_code':status,'confirmed':bool(row and row.failure_count >= 2)}


async def targeted_audit(db,site,job):
    from app.intelligence.audit import audit_page
    key=job.payload['resource_key']
    kind='woocommerce' if key.split(':')[0] in ('products','product_categories') else 'wordpress'
    async with await client_for(db,site,kind) as client:
        source=await client.read(key)
    page=store_page(db,site,source)
    response=await fetch(page.url)
    if response['status_code']!=200:
        raise ValueError('Changed page could not be verified publicly')
    observation=audit_page(page.url,response['html'],source)
    evidence=capture_html(site,job,page.url,response['html'],'targeted_audit')
    observation['signals'].update({'observed_at':iso(now()),'observation_type':'source_html','evidence':evidence})
    for finding in observation['findings']:
        finding['details']={**finding.get('details',{}),'evidence':evidence}
    upsert_observation(db,site,page,observation)
    db.commit()
    browser_job=enqueue(db,site,'browser',{'page_id':page.id},f'browser:{job.id}')
    return {'complete':True,'page_id':page.id,'browser_job_id':browser_job.id}


async def connection_test(db, site, job):
    kind = job.payload.get('kind', 'wordpress')
    row = find_connection(db, site.id, kind)
    if row is None or row.status == 'revoked' or not row.encrypted_credentials:
        raise ValueError('Connection is not configured')
    if kind in ('wordpress','woocommerce'):
        try:
            async with await client_for(db, site, kind) as client:
                result = await client.validate_connection()
                if kind == 'wordpress':
                    from app.authors import connection_fingerprint, unavailable
                    try:
                        import asyncio
                        authors = await asyncio.wait_for(client.discover_authors(), timeout=25)
                    except Exception:
                        authors = unavailable('author_discovery_failed')
                    result['author_discovery'] = {**authors, 'checked_at': iso(now()),
                                                  'connection_fingerprint': connection_fingerprint(row)}
        except Exception:
            row.status,row.checked_at='error',now()
            db.commit()
            raise
        row.capabilities = {**row.capabilities, **result}
        row.status = 'connected'
    elif kind in ('gsc', 'ga4'):
        # Google read-only connections are verified with the same provider
        # request used by scheduled visibility collection. Saving OAuth
        # material alone is not proof that the selected property is readable.
        from app.intelligence.visibility import collect

        secret, saved_config = credentials(db, site.id, kind)
        config = dict(saved_config or {})
        if kind == 'gsc':
            config.setdefault('site_url', site.origin)
        tested_at = now()
        try:
            provider_result = await collect(kind, secret, config)
        except Exception as exc:
            row.status = 'error'
            row.checked_at = tested_at
            row.capabilities = {
                **(row.capabilities or {}),
                'last_connection_test': {
                    'status': 'error',
                    'kind': kind,
                    'tested_at': iso(tested_at),
                    'error_type': type(exc).__name__,
                    'read_only': True,
                },
            }
            db.commit()
            raise ValueError(f'{kind} connection test failed: {type(exc).__name__}') from exc

        if not isinstance(provider_result, dict) or provider_result.get('status') != 'ok':
            error = provider_result.get('error') if isinstance(provider_result, dict) else None
            code = error.get('code') if isinstance(error, dict) else None
            if not isinstance(code, str) or not code.strip():
                code = 'provider_error'
            code = code.strip()[:64]
            row.status = 'error'
            row.checked_at = tested_at
            row.capabilities = {
                **(row.capabilities or {}),
                'last_connection_test': {
                    'status': 'error',
                    'kind': kind,
                    'tested_at': iso(tested_at),
                    'error_code': code,
                    'read_only': True,
                },
            }
            db.commit()
            raise ValueError(f'{kind} connection test failed: {code}')

        metadata = provider_result.get('metadata') if isinstance(provider_result.get('metadata'), dict) else {}
        row.capabilities = {
            **(row.capabilities or {}),
            'authenticated': True,
            'last_connection_test': {
                'status': 'verified',
                'kind': kind,
                'tested_at': iso(tested_at),
                'read_only': True,
                'token_refreshed': metadata.get('token_refreshed') is True,
            },
        }
        row.status = 'connected'
        row.checked_at = tested_at
        return {
            'kind': kind,
            'status': 'verified',
            'read_only': True,
            'message': f'{kind} read-only access verified',
        }
    elif kind == 'microsoft_graph':
        from app.connectors.microsoft_graph import MicrosoftGraphMailClient
        secret, config = credentials(db, site.id, kind)
        try:
            async with MicrosoftGraphMailClient(secret, config) as client:
                result = await client.validate_connection()
        except Exception:
            row.status, row.checked_at = 'error', now()
            row.capabilities = {**(row.capabilities or {}), 'last_connection_test': {
                'status': 'error', 'read_only': True, 'tested_at': iso(now()),
            }}
            db.commit()
            raise ValueError('Microsoft 365 authentication failed; review the connection settings') from None
        row.status, row.checked_at = 'connected', now()
        row.capabilities = {**(row.capabilities or {}), 'last_connection_test': {
            'status': 'authenticated', 'read_only': True, 'tested_at': iso(now()),
            'mailbox_authorization': 'unverified', 'delivery': 'not_tested',
        }}
        db.commit()
        return {'kind': kind, 'status': 'authenticated', 'read_only': True,
                'mailbox_authorization': 'unverified', 'delivery': 'not_tested',
                'message': 'Microsoft 365 authentication checked; no email was sent'}
    elif kind == 'ai':
        # An explicit OpenAI Responses configuration can be checked with the
        # provider's read-only model list.  This must not run a paid web-search
        # sample merely because an owner pressed Test.
        from app.intelligence.visibility import verify_ai_connection

        secret, saved_config = credentials(db, site.id, kind)
        config = dict(saved_config or {})
        tested_at = now()
        try:
            provider_result = await verify_ai_connection(secret, config)
        except Exception as exc:
            row.status = 'error'
            row.checked_at = tested_at
            row.capabilities = {
                **(row.capabilities or {}),
                'last_connection_test': {
                    'status': 'error',
                    'kind': kind,
                    'tested_at': iso(tested_at),
                    'error_type': type(exc).__name__,
                    'read_only': True,
                },
            }
            db.commit()
            raise ValueError(f'{kind} connection test failed: {type(exc).__name__}') from exc

        if not isinstance(provider_result, dict):
            provider_result = {
                'status': 'error',
                'error': {'code': 'invalid_response'},
            }
        result_status = provider_result.get('status')
        error = provider_result.get('error') if isinstance(provider_result.get('error'), dict) else {}
        code = error.get('code') if isinstance(error.get('code'), str) else 'provider_error'
        if result_status == 'unsupported':
            row.status = 'configured'
            row.checked_at = tested_at
            row.capabilities = {
                **(row.capabilities or {}),
                'last_connection_test': {
                    'status': 'unsupported',
                    'kind': kind,
                    'tested_at': iso(tested_at),
                    'error_code': code[:64],
                    'read_only': True,
                },
            }
            return {
                'kind': kind,
                'status': 'configured',
                'read_only': True,
                'message': 'AI credentials are saved; select an explicit OpenAI Responses format to verify the model',
            }
        if result_status != 'verified':
            row.status = 'error'
            row.checked_at = tested_at
            row.capabilities = {
                **(row.capabilities or {}),
                'last_connection_test': {
                    'status': 'error',
                    'kind': kind,
                    'tested_at': iso(tested_at),
                    'error_code': code[:64],
                    'read_only': True,
                },
            }
            db.commit()
            raise ValueError(f'{kind} connection test failed: {code[:64]}')

        model = provider_result.get('model')
        row.capabilities = {
            **(row.capabilities or {}),
            'authenticated': True,
            'model_available': provider_result.get('model_available') is True,
            'last_connection_test': {
                'status': 'verified',
                'kind': kind,
                'tested_at': iso(tested_at),
                'provider': 'OpenAI',
                'model': model if isinstance(model, str) else None,
                'model_available': provider_result.get('model_available') is True,
                'read_only': True,
            },
        }
        row.status = 'connected'
        row.checked_at = tested_at
        return {
            'kind': kind,
            'status': 'verified',
            'provider': 'OpenAI',
            'model': model if isinstance(model, str) else None,
            'model_available': provider_result.get('model_available') is True,
            'read_only': True,
            'message': 'OpenAI model access verified with a read-only check',
        }
    else:
        secret, config = credentials(db, site.id, kind)
        # Saving keys is not proof of provider access. A budgeted observation is the real test.
        if kind == 'smtp':
            row.status = 'configured'
            result = {'message':'Credentials saved; notification test requires configured recipients'}
        elif kind == 'dataforseo':
            login = secret.get('login') or secret.get('username')
            password = secret.get('password')
            if (
                not isinstance(login, str)
                or not login.strip()
                or not isinstance(password, str)
                or not password.strip()
            ):
                row.status = 'error'
                row.checked_at = now()
                row.capabilities = {
                    **(row.capabilities or {}),
                    'last_connection_test': {
                        'status': 'error',
                        'kind': kind,
                        'tested_at': iso(row.checked_at),
                        'error_code': 'missing_credentials',
                        'read_only': True,
                    },
                }
                db.commit()
                raise ValueError('dataforseo connection test requires a login and password')
            row.status = 'configured'
            row.capabilities = {
                **(row.capabilities or {}),
                'credential_shape_verified': True,
                'last_connection_test': {
                    'status': 'configured',
                    'kind': kind,
                    'tested_at': iso(now()),
                    'read_only': True,
                    'provider_access': 'pending_budgeted_observation',
                },
            }
            result = {'message':'DataForSEO credentials are shaped correctly; a budgeted observation will verify provider access'}
        else:
            row.status = 'configured'
            result = {'message':'Credentials decrypt successfully; run a budgeted collection to verify provider access'}
    row.checked_at = now()
    return result


_PLANNING_MEASUREMENT_KINDS = frozenset({'gsc', 'dataforseo', 'competitor_observation'})
_PLANNING_MEASUREMENT_LIMIT = 32
_PLANNING_INPUT_LIMIT = 48
_PLANNING_QUERY_LIMIT = 160


def _bounded_planning_text(value, *, limit=_PLANNING_QUERY_LIMIT):
    if not isinstance(value, str):
        return None
    value = ' '.join(value.split()).strip()
    if not value or len(value) > limit:
        return None
    return value


def _planning_measurement_inputs(db, site_id):
    """Project recent validated measurements into safe planning evidence.

    Provider response bodies are never passed to the topic planner. Only
    explicit query/domain fields are projected, with source and observation
    timestamps retained so a brief can explain why an input was considered.
    """

    measurements = db.scalars(
        select(Measurement).where(
            Measurement.site_id == site_id,
            Measurement.kind.in_(_PLANNING_MEASUREMENT_KINDS),
        ).order_by(Measurement.observed_at.desc()).limit(_PLANNING_MEASUREMENT_LIMIT)
    ).all()
    inputs = []
    seen = set()

    def add_search(measurement, value, *, kind='search_observation'):
        query = _bounded_planning_text(value)
        if not query or query.startswith(('/', 'http://', 'https://')):
            return
        key = (kind, query.casefold(), measurement.source)
        if key in seen:
            return
        seen.add(key)
        inputs.append({
            'kind': kind,
            'query': query,
            'source': str(measurement.source)[:128],
            'provider': str(measurement.source)[:128],
            'observed_at': iso(measurement.observed_at),
        })

    def add_competitor(measurement, data, domain):
        domain = _bounded_planning_text(domain, limit=253)
        if not domain or '://' in domain or '/' in domain:
            return
        target = _bounded_planning_text(data.get('target'), limit=253)
        key = ('competitor_observation', domain.casefold(), target or '')
        if key in seen:
            return
        seen.add(key)
        inputs.append({
            'kind': 'competitor_observation',
            'competitor_url': f'https://{domain}/',
            'target': target,
            'source': str(measurement.source)[:128],
            'provider': _bounded_planning_text(data.get('provider'), limit=128) or str(measurement.source)[:128],
            'observed_at': iso(measurement.observed_at),
        })

    for measurement in measurements:
        data = measurement.data if isinstance(measurement.data, dict) else {}
        if measurement.kind == 'gsc':
            rows = data.get('rows') if isinstance(data.get('rows'), list) else []
            for row in rows[:_PLANNING_INPUT_LIMIT]:
                if not isinstance(row, dict):
                    continue
                direct = row.get('query') or row.get('keyword') or row.get('search_query')
                if direct is not None:
                    add_search(measurement, direct)
                    continue
                keys = row.get('keys') if isinstance(row.get('keys'), list) else []
                for value in keys:
                    candidate = _bounded_planning_text(value)
                    if candidate and not candidate.startswith(('/', 'http://', 'https://')):
                        add_search(measurement, candidate)
                        break
        elif measurement.kind == 'dataforseo':
            # DataForSEO keeps the submitted keyword in task data. Read only
            # that bounded field; do not scan arbitrary provider result text.
            tasks = data.get('tasks') if isinstance(data.get('tasks'), list) else []
            for task in tasks[:_PLANNING_INPUT_LIMIT]:
                if not isinstance(task, dict):
                    continue
                task_data = task.get('data') if isinstance(task.get('data'), dict) else {}
                add_search(measurement, task_data.get('keyword') or task_data.get('query'))
                keywords = task_data.get('keywords') if isinstance(task_data.get('keywords'), list) else []
                for keyword in keywords[:_PLANNING_INPUT_LIMIT]:
                    add_search(measurement, keyword)
        elif measurement.kind == 'competitor_observation':
            competitors = data.get('competitors') if isinstance(data.get('competitors'), list) else []
            for competitor in competitors[:3]:
                add_competitor(measurement, data, competitor)
            if not competitors:
                add_competitor(measurement, data, data.get('competitor_url'))

        if len(inputs) >= _PLANNING_INPUT_LIMIT:
            break
    return inputs[:_PLANNING_INPUT_LIMIT]


async def plan(db, site, job):
    from app.intelligence.content import plan_topics
    pages = [record(p) for p in db.scalars(select(Page).where(Page.site_id == site.id))]
    policy = current_policy(db, site.id)
    keywords = policy.settings.get('tracked_keywords', []) if policy else []
    research_inputs = _planning_measurement_inputs(db, site.id)
    # Existing article bodies are never enrolled implicitly.  The planner
    # receives the full inventory for overlap detection and filters refresh
    # candidates by explicit enrollment.
    briefs = plan_topics(site.facts, pages, keywords,
                         [p for p in pages if p['resource_type'] == 'products'],
                         origin=site.origin,
                         research_inputs=research_inputs)
    existing = {a.title.lower().strip() for a in db.scalars(select(Article).where(Article.site_id == site.id))}
    requested_limit = job.payload.get('max_articles', 8) if isinstance(job.payload, dict) else 8
    if isinstance(requested_limit, bool) or not isinstance(requested_limit, int):
        requested_limit = 8
    # The normal planner keeps its historical eight-brief bound.  Bounded
    # workflows such as content_autopilot may request one deterministic brief
    # without changing the planner's default behavior for scheduled planning.
    requested_limit = max(1, min(8, requested_limit))
    created = []
    for brief in briefs[:requested_limit]:
        title = brief.get('title', '')
        if not title or title.lower().strip() in existing:
            continue
        article = Article(site_id=site.id, title=title, slug=re.sub(r'[^a-z0-9]+','-',title.lower()).strip('-')[:120],
                          brief=brief, sources=brief.get('sources',[]), author_id=(policy.settings.get('author_id') if policy else None),
                          status='planned', managed=True, updated_at=now())
        db.add(article)
        db.flush()
        created.append(article.id)
        existing.add(title.lower().strip())
    event(db, site, 'content_planned', f'{len(created)} article briefs added to the content plan')
    return {'article_ids':created,'count':len(created)}


_CONTENT_AUTOPILOT_MAX_ARTICLES = 1
_CONTENT_AUTOPILOT_STAGE_KINDS = {
    'plan': 'plan',
    'generate': 'generate',
    'publish': 'publish',
}


def _content_autopilot_unique(values: list[str]) -> list[str]:
    """Return stable, non-empty blocker codes without exposing source text."""

    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _content_autopilot_author_ids(site, policy, wordpress) -> tuple[set[str], str | None]:
    """Only a recent authenticated observation can verify the policy's choice."""
    from app.authors import author_ids, current_authors
    verified = author_ids(current_authors(wordpress))

    configured = None
    if policy is not None and isinstance(policy.settings, dict):
        value = policy.settings.get('author_id')
        if isinstance(value, (str, int)) and str(value).strip():
            configured = str(value).strip()
    if configured and configured in verified:
        return verified, configured
    return verified, None


def _content_autopilot_preflight(db, site) -> dict[str, Any]:
    """Check every local gate before content research, AI, or WordPress I/O.

    This helper intentionally reads encrypted credentials only to prove that
    they decrypt; it never returns them. Connection capability records are
    assertions from the last explicit verification, and the existing
    connectors still recheck remote permissions at every write.
    """

    policy = current_policy(db, site.id)
    controls = global_controls(db)
    blockers = list(evaluate_policy(
        site,
        policy,
        'publish',
        None,
        global_pause=bool(controls.get('global_pause')),
    ))
    if policy and policy.settings.get('publication_article_ids') is not None:
        blockers.append('restricted_publication_scope')
    wordpress = find_connection(db, site.id, 'wordpress')
    wordpress_credentials = False
    if wordpress is None or wordpress.status == 'revoked' or not wordpress.encrypted_credentials:
        blockers.append('wordpress_connection_required')
    elif wordpress.status != 'connected':
        blockers.append('wordpress_connection_unverified')
    else:
        try:
            secret, _ = credentials(db, site.id, 'wordpress')
            wordpress_credentials = isinstance(secret, dict) and bool(secret)
        except Exception:
            blockers.append('wordpress_credentials_invalid')
        if not wordpress_credentials and 'wordpress_credentials_invalid' not in blockers:
            blockers.append('wordpress_credentials_missing')
        capabilities = wordpress.capabilities if isinstance(wordpress.capabilities, dict) else {}
        native = capabilities.get('native') if isinstance(capabilities.get('native'), dict) else {}
        if capabilities.get('authenticated') is not True:
            blockers.append('wordpress_capabilities_unverified')
        if native.get('create') is not True:
            blockers.append('wordpress_post_creation_not_verified')
        if native.get('publish') is not True:
            blockers.append('wordpress_publish_not_verified')

    ai = find_connection(db, site.id, 'ai')
    ai_config: dict[str, Any] = {}
    ai_credentials = False
    if ai is None or ai.status == 'revoked' or not ai.encrypted_credentials:
        blockers.append('ai_connection_required')
    elif ai.status != 'connected':
        blockers.append('ai_connection_unverified')
    else:
        try:
            secret, saved_config = credentials(db, site.id, 'ai')
            ai_credentials = isinstance(secret, dict) and bool(secret)
            ai_config = saved_config if isinstance(saved_config, dict) else {}
        except Exception:
            blockers.append('ai_credentials_invalid')
        if not ai_credentials and 'ai_credentials_invalid' not in blockers:
            blockers.append('ai_credentials_missing')
        endpoint = ai_config.get('endpoint') or ai_config.get('base_url') or ai_config.get('url')
        if not isinstance(endpoint, str) or not endpoint.strip():
            blockers.append('ai_endpoint_missing')
        else:
            try:
                parsed = urlsplit(endpoint.strip())
                if (
                    parsed.scheme.lower() not in {'http', 'https'}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    blockers.append('ai_endpoint_invalid')
            except ValueError:
                blockers.append('ai_endpoint_invalid')
        if not isinstance(ai_config.get('model'), str) or not ai_config.get('model', '').strip():
            blockers.append('ai_model_missing')
        maximum = ai_config.get('max_cost_cents')
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            blockers.append('ai_max_cost_unknown')
        estimate = ai_config.get('estimated_cost_cents')
        if (
            isinstance(estimate, bool)
            or not isinstance(estimate, int)
            or estimate < 0
            or (
                isinstance(maximum, int)
                and not isinstance(maximum, bool)
                and estimate > maximum
            )
        ):
            blockers.append('ai_cost_estimate_invalid')

    verified_author_ids, default_author_id = _content_autopilot_author_ids(site, policy, wordpress)
    if not verified_author_ids:
        blockers.append('author_unverified')
    elif policy is not None and isinstance(policy.settings, dict):
        configured_author = policy.settings.get('author_id')
        if configured_author and str(configured_author).strip() not in verified_author_ids:
            blockers.append('author_unverified')

    return {
        'policy': policy,
        'policy_version': policy.version if policy is not None and isinstance(policy.version, int) else None,
        'blockers': _content_autopilot_unique(blockers),
        'verified_author_ids': verified_author_ids,
        'default_author_id': default_author_id,
    }


def _content_autopilot_weekly_blocker(db, site, policy) -> str | None:
    """Apply the same local-week publication count used by ``publish``."""

    if policy is None or not isinstance(policy.settings, dict):
        return None
    posts_per_week = policy.settings.get('posts_per_week', 2)
    if isinstance(posts_per_week, bool) or not isinstance(posts_per_week, int):
        return 'policy_invalid'
    from datetime import timezone
    from zoneinfo import ZoneInfo

    local_now = now().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(site.timezone))
    week_start = (
        (local_now - timedelta(days=local_now.weekday()))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
        .replace(tzinfo=None)
    )
    published = db.scalar(select(func.count()).select_from(Publication).where(
        Publication.site_id == site.id,
        Publication.article_id.is_not(None),
        Publication.status == 'published',
        Publication.created_at >= week_start,
    ))
    if int(published or 0) >= max(0, posts_per_week):
        return 'weekly_publication_limit_reached'
    return None


def _content_autopilot_child_job(db, site, parent_job, stage: str, article_id: str | None = None) -> Job:
    """Create or reuse one deterministic child Job for an autopilot stage."""

    if stage not in _CONTENT_AUTOPILOT_STAGE_KINDS:
        raise ValueError('Unsupported content autopilot stage')
    article_key = article_id or 'none'
    child_id = hashlib.sha256(
        f'content_autopilot:{site.id}:{parent_job.id}:{stage}:{article_key}'.encode('utf-8')
    ).hexdigest()[:32]
    kind = _CONTENT_AUTOPILOT_STAGE_KINDS[stage]
    row = db.get(Job, child_id)
    if row is not None:
        if row.site_id != site.id or row.kind != kind:
            raise ValueError('Content-autopilot child identity conflict')
        return row
    payload = {
        'content_autopilot_parent_job_id': parent_job.id,
        'content_autopilot_stage': stage,
    }
    if article_id:
        payload['article_id'] = article_id
    if stage == 'plan':
        payload['max_articles'] = _CONTENT_AUTOPILOT_MAX_ARTICLES
    timestamp = now()
    row = Job(
        id=child_id,
        site_id=site.id,
        kind=kind,
        status='queued',
        payload=payload,
        result={},
        idempotency_key=f'{site.id}:content-autopilot:{parent_job.id}:{stage}:{article_key}',
        available_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )
    db.add(row)
    db.flush()
    return row


def _content_autopilot_safe_stage(stage: str, status: str, *, child_id: str | None = None,
                                  article_id: str | None = None, blockers: list[str] | None = None,
                                  **details) -> dict[str, Any]:
    """Build a bounded stage record that is safe for browser-facing results."""

    result = {
        'name': stage,
        'status': status,
        'blockers': _content_autopilot_unique(blockers or []),
    }
    if child_id:
        result['child_job_id'] = child_id
    if article_id:
        result['article_id'] = article_id
    for key, value in details.items():
        if key in {'source', 'checks_passed', 'source_count', 'reservation_status', 'verified'}:
            result[key] = value
    return result


def _content_autopilot_finish(db, site, job, result: dict[str, Any]) -> dict[str, Any]:
    """Persist the terminal audit event and return a safe parent result."""

    job.result = result
    job.updated_at = now()
    event_data = {
        'job_id': job.id,
        'article_id': result.get('article_id'),
        'status': result.get('status'),
        'policy_version': result.get('policy_version'),
        'blocker_count': len(result.get('blockers', [])),
    }
    event(db, site, 'content_autopilot_finished', 'Content autopilot finished', event_data)
    db.commit()
    return result


def _content_autopilot_base(job, preflight, *, status: str, stages: list[dict[str, Any]],
                            article_id: str | None = None, blockers: list[str] | None = None,
                            next_action: str = 'review_content_autopilot_result', complete: bool = False) -> dict[str, Any]:
    return {
        'workflow': 'content_autopilot',
        'job_id': job.id,
        'status': status,
        'complete': complete,
        'policy_version': preflight.get('policy_version'),
        'article_id': article_id,
        'stages': stages[:6],
        'blockers': _content_autopilot_unique(blockers or []),
        'next_action': next_action,
    }


async def content_autopilot(db, site, job):
    """Run one policy-governed article from plan through verified publication.

    The parent job never dispatches child workers. Instead, it records
    deterministic child Job rows and invokes the already-authorized plan,
    generate, and publish handlers in order. Persisted research, child
    results, the budget operation key, and the Publication operation key make
    a repeated parent invocation resume rather than duplicate work.
    """

    # Worker retries pass the durable parent row back to the handler. A
    # completed publication is already verified work, so replaying the same
    # parent must not plan a second article merely because the original one
    # is no longer in ``planned`` state.
    if (
        isinstance(job.result, dict)
        and job.result.get('workflow') == 'content_autopilot'
        and job.result.get('status') == 'published'
    ):
        event(db, site, 'content_autopilot_replayed', 'Content autopilot publication result replayed', {
            'job_id': job.id,
            'article_id': job.result.get('article_id'),
            'status': 'published',
        })
        db.commit()
        return job.result

    preflight = _content_autopilot_preflight(db, site)
    if preflight['blockers'] and set(preflight['blockers']) == {'author_unverified'}:
        # Preserve local pause/budget gates before making even a read-only
        # connection call. Old facts and inventory are never author authority.
        from app.authors import refresh_authors
        await refresh_authors(db, site)
        preflight = _content_autopilot_preflight(db, site)
    event(db, site, 'content_autopilot_started', 'Content autopilot started', {
        'job_id': job.id,
        'policy_version': preflight.get('policy_version'),
    })
    db.commit()
    if preflight['blockers']:
        event(db, site, 'content_autopilot_gated', 'Content autopilot is gated', {
            'job_id': job.id,
            'policy_version': preflight.get('policy_version'),
            'blockers': preflight['blockers'],
        })
        result = _content_autopilot_base(
            job,
            preflight,
            status='gated',
            stages=[_content_autopilot_safe_stage('preflight', 'gated', blockers=preflight['blockers'])],
            blockers=preflight['blockers'],
            next_action='resolve_content_autopilot_blockers',
        )
        return _content_autopilot_finish(db, site, job, result)

    policy = preflight['policy']
    weekly_blocker = _content_autopilot_weekly_blocker(db, site, policy)
    if weekly_blocker:
        event(db, site, 'content_autopilot_gated', 'Content autopilot publication quota is full', {
            'job_id': job.id,
            'policy_version': preflight.get('policy_version'),
            'blockers': [weekly_blocker],
        })
        result = _content_autopilot_base(
            job,
            preflight,
            status='gated',
            stages=[
                _content_autopilot_safe_stage('preflight', 'passed'),
                _content_autopilot_safe_stage('publication_quota', 'gated', blockers=[weekly_blocker]),
            ],
            blockers=[weekly_blocker],
            next_action='wait_for_next_publication_week',
        )
        return _content_autopilot_finish(db, site, job, result)

    payload = job.payload if isinstance(job.payload, dict) else {}
    requested_article_id = payload.get('article_id')
    if requested_article_id is not None and (
        not isinstance(requested_article_id, str) or not requested_article_id.strip()
    ):
        result = _content_autopilot_base(
            job,
            preflight,
            status='gated',
            stages=[_content_autopilot_safe_stage(
                'article_selection', 'gated', blockers=['article_id_invalid'],
            )],
            blockers=['article_id_invalid'],
            next_action='select_a_managed_planned_article',
        )
        return _content_autopilot_finish(db, site, job, result)

    article = None
    planning_stage = None
    if requested_article_id:
        article = db.scalar(select(Article).where(
            Article.site_id == site.id,
            Article.id == requested_article_id,
        ))
        blockers = [] if article is not None else ['article_not_found_or_cross_site']
    else:
        article = db.scalars(select(Article).where(
            Article.site_id == site.id,
            Article.status == 'planned',
            Article.managed.is_(True),
        ).order_by(Article.created_at, Article.id).limit(1)).first()
        blockers = []

    if article is not None:
        if article.status != 'planned':
            blockers.append('article_not_planned')
        if article.managed is not True:
            blockers.append('article_not_managed')
        if isinstance(article.brief, dict) and article.brief.get('purpose') == 'refresh_existing':
            blockers.append('refresh_article_not_supported')
    if blockers:
        result = _content_autopilot_base(
            job,
            preflight,
            status='gated',
            stages=[_content_autopilot_safe_stage(
                'article_selection', 'gated', blockers=blockers,
            )],
            blockers=blockers,
            next_action='select_a_managed_planned_article',
        )
        return _content_autopilot_finish(db, site, job, result)

    if article is None:
        plan_job = _content_autopilot_child_job(db, site, job, 'plan')
        planned_ids = None
        if plan_job.status == 'complete' and isinstance(plan_job.result, dict):
            planned_ids = plan_job.result.get('article_ids')
        else:
            plan_job.status = 'running'
            plan_job.attempts = (plan_job.attempts or 0) + 1
            plan_job.lease_until = now() + timedelta(minutes=16)
            plan_job.updated_at = now()
            db.commit()
            try:
                planned = await plan(db, site, plan_job)
                article_ids = planned.get('article_ids', [])
                if not isinstance(article_ids, list):
                    article_ids = []
                article_ids = [value for value in article_ids if isinstance(value, str)]
                plan_job.status = 'complete'
                plan_job.result = {
                    'status': 'planned',
                    'article_ids': article_ids[:_CONTENT_AUTOPILOT_MAX_ARTICLES],
                    'count': min(int(planned.get('count', 0)), _CONTENT_AUTOPILOT_MAX_ARTICLES),
                }
                plan_job.lease_until = None
                plan_job.updated_at = now()
                db.commit()
                planned_ids = plan_job.result.get('article_ids')
            except Exception:
                db.rollback()
                plan_job = db.get(Job, plan_job.id)
                if plan_job is not None:
                    plan_job.status = 'failed'
                    plan_job.result = {'status': 'failed', 'blockers': ['planning_failed']}
                    plan_job.lease_until = None
                    plan_job.updated_at = now()
                    db.commit()
                result = _content_autopilot_base(
                    job,
                    preflight,
                    status='failed',
                    stages=[_content_autopilot_safe_stage(
                        'plan', 'failed',
                        child_id=plan_job.id if plan_job else None,
                        blockers=['planning_failed'],
                    )],
                    blockers=['planning_failed'],
                    next_action='review_content_plan',
                )
                return _content_autopilot_finish(db, site, job, result)
        planned_ids = planned_ids if isinstance(planned_ids, list) else []
        planned_ids = [value for value in planned_ids if isinstance(value, str)]
        article = db.scalar(select(Article).where(
            Article.site_id == site.id,
            Article.id.in_(planned_ids),
            Article.status == 'planned',
            Article.managed.is_(True),
        ).order_by(Article.created_at, Article.id).limit(1)) if planned_ids else None
        planning_stage = _content_autopilot_safe_stage(
            'plan',
            'planned' if article is not None else 'needs_review',
            child_id=plan_job.id,
            article_id=article.id if article is not None else None,
            blockers=[] if article is not None else ['no_eligible_planned_article'],
            source='planner',
        )
        if article is None:
            result = _content_autopilot_base(
                job,
                preflight,
                status='needs_review',
                stages=[planning_stage],
                blockers=['no_eligible_planned_article'],
                next_action='review_content_plan',
            )
            return _content_autopilot_finish(db, site, job, result)
    else:
        planning_stage = _content_autopilot_safe_stage(
            'plan', 'planned', article_id=article.id, source='existing_planned_article',
        )

    author_id = article.author_id or preflight.get('default_author_id')
    if not author_id or str(author_id) not in preflight['verified_author_ids']:
        blocker = 'author_unverified'
        result = _content_autopilot_base(
            job,
            preflight,
            status='gated',
            stages=[
                planning_stage,
                _content_autopilot_safe_stage(
                    'author', 'gated', article_id=article.id, blockers=[blocker],
                ),
            ],
            article_id=article.id,
            blockers=[blocker],
            next_action='verify_and_select_wordpress_author',
        )
        return _content_autopilot_finish(db, site, job, result)
    if article.author_id != str(author_id):
        article.author_id = str(author_id)
        article.updated_at = now()
        db.commit()

    stages = [planning_stage]
    brief = dict(article.brief or {})
    research = brief.get('research') if isinstance(brief.get('research'), dict) else None
    if not research or research.get('complete') is not True:
        event(db, site, 'content_autopilot_research_started', 'Content autopilot research started', {
            'job_id': job.id, 'article_id': article.id,
        })
        db.commit()
        from app.intelligence.research import research_brief
        try:
            research = await research_brief(brief, site.facts or {})
        except Exception:
            article.status = 'review_needed'
            article.checks = {'passed': False, 'blockers': ['research_review_required'], 'warnings': []}
            db.commit()
            event(db, site, 'content_autopilot_generation', 'Content autopilot research needs review', {
                'job_id': job.id, 'article_id': article.id, 'status': 'needs_review',
            })
            stages.append(_content_autopilot_safe_stage(
                'research', 'needs_review', article_id=article.id, blockers=['research_failed'],
            ))
            result = _content_autopilot_base(
                job,
                preflight,
                status='needs_review',
                stages=stages,
                article_id=article.id,
                blockers=['research_failed'],
                next_action='review_research_evidence',
            )
            return _content_autopilot_finish(db, site, job, result)
        article.brief = {**brief, 'research': research}
        if isinstance(research, dict) and isinstance(research.get('sources'), list) and research['sources']:
            article.sources = research['sources']
        if not isinstance(research, dict) or research.get('complete') is not True:
            article.status = 'review_needed'
            research_blockers = ['research_review_required']
            if isinstance(research, dict) and isinstance(research.get('blockers'), list):
                research_blockers.extend(
                    str(value) for value in research['blockers'][:8] if str(value).strip()
                )
            article.checks = {
                'passed': False,
                'blockers': _content_autopilot_unique(research_blockers),
                'warnings': [],
            }
            db.commit()
            stages.append(_content_autopilot_safe_stage(
                'research', 'needs_review', article_id=article.id,
                blockers=['research_review_required'],
                source_count=(
                    len(research.get('sources', []))
                    if isinstance(research, dict) and isinstance(research.get('sources'), list)
                    else 0
                ),
            ))
            result = _content_autopilot_base(
                job,
                preflight,
                status='needs_review',
                stages=stages,
                article_id=article.id,
                blockers=['research_review_required'],
                next_action='review_research_evidence',
            )
            return _content_autopilot_finish(db, site, job, result)
        article.updated_at = now()
        db.commit()
    stages.append(_content_autopilot_safe_stage(
        'research',
        'complete',
        article_id=article.id,
        source_count=len(article.sources) if isinstance(article.sources, list) else 0,
    ))

    generate_job = _content_autopilot_child_job(db, site, job, 'generate', article.id)
    generation_result = (
        generate_job.result
        if generate_job.status == 'complete' and isinstance(generate_job.result, dict)
        else None
    )
    if generate_job.status in {'failed', 'blocked', 'needs_reconciliation'}:
        stages.append(_content_autopilot_safe_stage(
            'generate', 'needs_review', child_id=generate_job.id,
            article_id=article.id, blockers=['generation_failed'],
        ))
        result = _content_autopilot_base(
            job,
            preflight,
            status='needs_review',
            stages=stages,
            article_id=article.id,
            blockers=['generation_failed'],
            next_action='review_or_reconcile_generation',
        )
        return _content_autopilot_finish(db, site, job, result)

    if generation_result and generation_result.get('status') not in {None, 'checked'}:
        stages.append(_content_autopilot_safe_stage(
            'generate', 'needs_review', child_id=generate_job.id,
            article_id=article.id, blockers=['editorial_check_failed'], checks_passed=False,
        ))
        result = _content_autopilot_base(
            job,
            preflight,
            status='needs_review',
            stages=stages,
            article_id=article.id,
            blockers=['editorial_check_failed'],
            next_action='review_editorial_checks',
        )
        return _content_autopilot_finish(db, site, job, result)

    if not (generation_result and generation_result.get('status') == 'checked'):
        if article.status == 'checked' and isinstance(article.checks, dict) and article.checks.get('passed') is True:
            generation_result = {'status': 'checked', 'checks': article.checks}
            generate_job.status = 'complete'
            generate_job.result = generation_result
            generate_job.updated_at = now()
            db.commit()
        else:
            generate_job.status = 'running'
            generate_job.attempts = (generate_job.attempts or 0) + 1
            generate_job.lease_until = now() + timedelta(minutes=16)
            generate_job.updated_at = now()
            db.commit()
            event(db, site, 'content_autopilot_generation_started', 'Content autopilot generation started', {
                'job_id': job.id,
                'article_id': article.id,
                'child_job_id': generate_job.id,
            })
            try:
                generation_result = await generate(db, site, generate_job)
            except Exception:
                db.rollback()
                generate_job = db.get(Job, generate_job.id)
                article = db.get(Article, article.id)
                if generate_job is not None:
                    generate_job.status = 'failed'
                    generate_job.result = {'status': 'failed', 'blockers': ['generation_failed']}
                    generate_job.lease_until = None
                    generate_job.updated_at = now()
                db.commit()
                event(db, site, 'content_autopilot_generation_finished', 'Content autopilot generation needs review', {
                    'job_id': job.id,
                    'article_id': article.id if article else None,
                    'status': 'needs_review',
                })
                stages.append(_content_autopilot_safe_stage(
                    'generate', 'needs_review',
                    child_id=generate_job.id if generate_job else None,
                    article_id=article.id if article else None,
                    blockers=['generation_failed'],
                ))
                result = _content_autopilot_base(
                    job,
                    preflight,
                    status='needs_review',
                    stages=stages,
                    article_id=article.id if article else None,
                    blockers=['generation_failed'],
                    next_action='review_or_reconcile_generation',
                )
                return _content_autopilot_finish(db, site, job, result)
            article = db.get(Article, article.id)
            if generation_result.get('status') == 'checked' and article is not None and isinstance(article.checks, dict) and article.checks.get('passed') is True:
                generate_job.status = 'complete'
                generate_job.result = {
                    'status': 'checked',
                    'checks': article.checks,
                    'cost_status': generation_result.get('cost_status'),
                }
            else:
                generate_job.status = 'complete'
                generate_job.result = {
                    'status': 'needs_review',
                    'checks': article.checks if article is not None else {},
                }
            generate_job.lease_until = None
            generate_job.updated_at = now()
            db.commit()
            event(db, site, 'content_autopilot_generation_finished', 'Content autopilot generation finished', {
                'job_id': job.id,
                'article_id': article.id if article else None,
                'status': generate_job.result.get('status'),
            })

    article = db.get(Article, article.id)
    if article is None or article.status != 'checked' or not isinstance(article.checks, dict) or article.checks.get('passed') is not True:
        blocker = 'editorial_check_failed'
        stages.append(_content_autopilot_safe_stage(
            'generate', 'needs_review', child_id=generate_job.id,
            article_id=article.id if article else None, blockers=[blocker], checks_passed=False,
        ))
        result = _content_autopilot_base(
            job,
            preflight,
            status='needs_review',
            stages=stages,
            article_id=article.id if article else None,
            blockers=[blocker],
            next_action='review_editorial_checks',
        )
        return _content_autopilot_finish(db, site, job, result)
    stages.append(_content_autopilot_safe_stage(
        'generate',
        'generated_checked',
        child_id=generate_job.id,
        article_id=article.id,
        checks_passed=True,
        reservation_status=(generation_result or {}).get('cost_status'),
    ))

    # Recheck every policy, pause, connection, author, and cost gate after
    # paid generation and immediately before the write-capable handler.
    publication_preflight = _content_autopilot_preflight(db, site)
    if publication_preflight['blockers']:
        blockers = publication_preflight['blockers']
        stages.append(_content_autopilot_safe_stage(
            'publication', 'gated', article_id=article.id, blockers=blockers,
        ))
        result = _content_autopilot_base(
            job,
            publication_preflight,
            status='gated',
            stages=stages,
            article_id=article.id,
            blockers=blockers,
            next_action='resolve_publication_blockers',
        )
        return _content_autopilot_finish(db, site, job, result)
    weekly_blocker = _content_autopilot_weekly_blocker(db, site, publication_preflight['policy'])
    if weekly_blocker:
        stages.append(_content_autopilot_safe_stage(
            'publication', 'gated', article_id=article.id, blockers=[weekly_blocker],
        ))
        result = _content_autopilot_base(
            job,
            publication_preflight,
            status='gated',
            stages=stages,
            article_id=article.id,
            blockers=[weekly_blocker],
            next_action='wait_for_next_publication_week',
        )
        return _content_autopilot_finish(db, site, job, result)

    publish_job = _content_autopilot_child_job(db, site, job, 'publish', article.id)
    publication_result = (
        publish_job.result
        if publish_job.status == 'complete' and isinstance(publish_job.result, dict)
        else None
    )
    if publish_job.status in {'failed', 'blocked', 'needs_reconciliation'}:
        status = 'ambiguous' if publish_job.result.get('status') == 'ambiguous' else 'failed'
        blocker = 'publication_ambiguous' if status == 'ambiguous' else 'publication_failed'
        stages.append(_content_autopilot_safe_stage(
            'publication', status, child_id=publish_job.id,
            article_id=article.id, blockers=[blocker],
        ))
        result = _content_autopilot_base(
            job,
            publication_preflight,
            status=status,
            stages=stages,
            article_id=article.id,
            blockers=[blocker],
            next_action='reconcile_publication' if status == 'ambiguous' else 'review_publication_failure',
        )
        return _content_autopilot_finish(db, site, job, result)
    if not (publication_result and publication_result.get('status') == 'published'):
        publish_job.status = 'running'
        publish_job.attempts = (publish_job.attempts or 0) + 1
        publish_job.lease_until = now() + timedelta(minutes=16)
        publish_job.updated_at = now()
        db.commit()
        event(db, site, 'content_autopilot_publication_started', 'Content autopilot publication started', {
            'job_id': job.id,
            'article_id': article.id,
            'child_job_id': publish_job.id,
        })
        try:
            publication_result = await publish(db, site, publish_job)
        except Exception:
            db.rollback()
            publish_job = db.get(Job, publish_job.id)
            article = db.get(Article, article.id)
            publication = db.scalar(select(Publication).where(
                Publication.site_id == site.id,
                Publication.article_id == article.id,
                Publication.operation_key == f'publish:{site.id}:{article.id}',
            )) if article is not None else None
            status = 'ambiguous' if publication is not None and publication.status == 'ambiguous' else 'failed'
            blocker = 'publication_ambiguous' if status == 'ambiguous' else 'publication_failed'
            if publish_job is not None:
                publish_job.status = 'needs_reconciliation' if status == 'ambiguous' else 'failed'
                publish_job.result = {'status': status, 'blockers': [blocker]}
                publish_job.lease_until = None
                publish_job.updated_at = now()
            db.commit()
            event(db, site, 'content_autopilot_publication_finished', 'Content autopilot publication needs attention', {
                'job_id': job.id,
                'article_id': article.id if article else None,
                'status': status,
            })
            stages.append(_content_autopilot_safe_stage(
                'publication', status,
                child_id=publish_job.id if publish_job else None,
                article_id=article.id if article else None,
                blockers=[blocker],
            ))
            result = _content_autopilot_base(
                job,
                publication_preflight,
                status=status,
                stages=stages,
                article_id=article.id if article else None,
                blockers=[blocker],
                next_action='reconcile_publication' if status == 'ambiguous' else 'review_publication_failure',
            )
            return _content_autopilot_finish(db, site, job, result)
        publish_job.status = 'complete' if publication_result.get('status') == 'published' else 'failed'
        publish_job.result = {
            'status': publication_result.get('status', 'failed'),
            'verified': publication_result.get('status') == 'published',
        }
        publish_job.lease_until = None
        publish_job.updated_at = now()
        db.commit()
        event(db, site, 'content_autopilot_publication_finished', 'Content autopilot publication finished', {
            'job_id': job.id,
            'article_id': article.id,
            'status': publish_job.result['status'],
        })
    if not publication_result or publication_result.get('status') != 'published':
        blocker = 'publication_failed'
        stages.append(_content_autopilot_safe_stage(
            'publication', 'failed', child_id=publish_job.id,
            article_id=article.id, blockers=[blocker],
        ))
        result = _content_autopilot_base(
            job,
            publication_preflight,
            status='failed',
            stages=stages,
            article_id=article.id,
            blockers=[blocker],
            next_action='review_publication_failure',
        )
        return _content_autopilot_finish(db, site, job, result)

    stages.append(_content_autopilot_safe_stage(
        'publication', 'published', child_id=publish_job.id,
        article_id=article.id, verified=True,
    ))
    result = _content_autopilot_base(
        job,
        publication_preflight,
        status='published',
        stages=stages,
        article_id=article.id,
        next_action='monitor_published_article',
        complete=True,
    )
    return _content_autopilot_finish(db, site, job, result)


def paid_reservation(db, site, operation_key, config):
    estimate = config.get('max_cost_cents',config.get('estimated_cost_cents'))
    if not isinstance(estimate, int) or isinstance(estimate, bool) or estimate <= 0:
        raise ValueError('Enter a known maximum request cost before running paid work')
    policy = current_policy(db, site.id)
    row = reserve(db, site.id, operation_key, estimate,
                  policy.settings.get('monthly_budget_cents',30000) if policy else 30000)
    db.commit()
    return row


def reconcile_provider_cost(db,site,reservation,result):
    if result.get('cost_basis')=='provider_actual':
        settle(db,reservation.id,result['cost_cents'])
        return 'settled'
    event(db,site,'cost_reconciliation_needed','Provider did not return an actual charge; maximum cost remains reserved',
          {'reservation_id':reservation.id,'cost_basis':result.get('cost_basis','unknown'),'usage':result.get('usage')})
    return 'reserved_pending_actual_cost'


async def generate(db, site, job):
    from app.intelligence.content import generate_article, check_article
    from app.intelligence.research import research_brief
    article = scoped(db, Article, job.payload.get('article_id'), site)
    if article.status in ('published','publishing','verifying'):
        raise ValueError('Published articles require the enrolled refresh workflow')
    brief = dict(article.brief or {})
    research = brief.get('research') if isinstance(brief.get('research'), dict) else None
    if not research or research.get('complete') is not True:
        try:
            research = await research_brief(brief, site.facts or {})
        except Exception as exc:
            article.status = 'review_needed'
            article.brief = {**brief, 'research': {'complete': False, 'blockers': ['research_failed'],
                                                     'research_notes': [{'kind': 'research', 'status': 'error',
                                                                         'reason': type(exc).__name__}], 'sources': []}}
            article.checks = {'passed': False, 'blockers': ['research_review_required'], 'warnings': []}
            db.commit()
            raise ValueError('Research needs review before generation') from exc
        article.brief = {**brief, 'research': research}
        if isinstance(research.get('sources'), list) and research['sources']:
            article.sources = research['sources']
        if research.get('complete') is not True:
            article.status = 'review_needed'
            article.checks = {'passed': False,
                              'blockers': ['research_review_required', *research.get('blockers', [])],
                              'warnings': [], 'research': research}
            db.commit()
            raise ValueError('Research needs review before generation')
        db.commit()
    secret, config = credentials(db, site.id, 'ai')
    reservation = paid_reservation(db, site, 'generate:' + job.id, config)
    article.status = 'drafting'
    db.commit()
    try:
        generated = await generate_article({**article.brief,'title':article.title,'sources':article.sources}, site.facts, {**config,**secret})
        if generated.get('status') == 'error':
            if generated.get('cost_basis') == 'provider_actual' or generated.get('usage'):
                reconcile_provider_cost(db,site,reservation,generated)
            raise ValueError('Generation needs attention: '+generated.get('error',{}).get('code','provider_error'))
    except Exception:
        # A timeout can have incurred charges: preserve reservation until reconciled.
        article.status = 'review_needed'
        article.checks = {'passed':False,'blockers':['Generation failed; inspect provider connection and billing before retrying']}
        db.commit()
        raise
    cost_status = reconcile_provider_cost(db,site,reservation,generated)
    db.add(Revision(site_id=site.id, article_id=article.id, title=article.title, body=article.body, reason='before_generation'))
    article.body = generated.get('body', generated.get('content',''))
    article.sources = generated.get('sources',article.sources)
    article.brief = {**article.brief, 'generation':generated.get('provenance',{})}
    article.updated_at = now()
    article.checks = await checked_article(db, site, article)
    article.status = 'checked' if article.checks['passed'] else 'review_needed'
    event(db, site, 'article_generated', f'Draft prepared: {article.title}', {'article_id':article.id,'checks':article.checks})
    return {'article_id':article.id, 'status':article.status,'checks':article.checks,'cost_status':cost_status,'reservation_id':reservation.id}


def publication(db, site, operation_key, policy, article=None, candidate=None):
    existing = db.scalar(select(Publication).where(Publication.operation_key == operation_key))
    if existing:
        return existing
    row = Publication(site_id=site.id, article_id=article.id if article else None,
                      candidate_id=candidate.id if candidate else None, operation_key=operation_key,
                      status='preparing', policy_version=policy.version, snapshot={}, result={},updated_at=now())
    db.add(row)
    db.flush()
    return row


_PUBLICATION_RECONCILIATION_STATUSES = frozenset({'ambiguous', 'needs_reconciliation'})


def _publication_requires_reconciliation(row: Publication) -> bool:
    """Return whether a publication may have an unknown remote outcome."""

    result = row.result if isinstance(row.result, dict) else {}
    if row.status in _PUBLICATION_RECONCILIATION_STATUSES:
        return True
    # Preserve compatibility with older rows that recorded an ambiguous
    # connector exception as a generic failed publication.
    return (
        row.status == 'failed'
        and (
            result.get('status') == 'ambiguous'
            or result.get('error_type') == 'AmbiguousOutcome'
        )
    )


def _publication_article_payload(article: Article) -> dict[str, Any]:
    """Build the bounded editorial payload used by read-only draft lookup."""

    return {
        'title': article.title,
        'body': article.body or '',
        'slug': article.slug,
        'author_id': article.author_id,
    }


def _publication_article_matches_snapshot(article: Article, snapshot: dict[str, Any] | None) -> bool:
    """Keep a later local edit from being accepted as an old remote outcome."""

    if not isinstance(snapshot, dict):
        return True
    for field in ('title', 'body', 'slug', 'author_id'):
        expected = snapshot.get(field)
        if expected is None:
            continue
        actual = getattr(article, field, None)
        if str(actual or '') != str(expected or ''):
            return False
    return True


def _publication_remote_matches(
    client,
    current: dict[str, Any],
    article: Article,
    expected: dict[str, Any] | None,
) -> bool:
    """Compare only the approved editorial fields, never provider payloads."""

    if expected:
        try:
            return bool(client.matches_snapshot(current, expected, ignore_status=True))
        except Exception:
            # An available snapshot must match in full. A malformed/legacy
            # snapshot needs review, not a weaker comparison of fewer fields.
            return False

    expected_values = {
        'title': article.title,
        'body': article.body or '',
        'slug': article.slug,
        'author_id': article.author_id,
    }
    for field, value in expected_values.items():
        if value in (None, ''):
            continue
        metadata = current.get('metadata') if isinstance(current.get('metadata'), dict) else {}
        actual = current.get(field, metadata.get(field))
        if field == 'author_id':
            actual = current.get('author_id', current.get('author', metadata.get('author_id')))
        if str(actual or '') != str(value):
            return False
    return bool(expected_values['title'] and expected_values['body'])


async def _verify_reconciled_publication(site, job, article, current):
    """Verify an already-published remote record using public read-only checks."""

    if current.get('status') != 'publish':
        return {'status': 'draft', 'reason': 'remote_record_is_not_published'}
    url = current.get('url')
    if not isinstance(url, str) or not url.strip():
        return {'status': 'held', 'reason': 'published_record_has_no_public_url'}
    try:
        response = await fetch(url)
        html = response.get('html', '') if isinstance(response, dict) else ''
        status_code = response.get('status_code') if isinstance(response, dict) else None
        if not isinstance(html, str) or status_code != 200:
            return {'status': 'held', 'reason': 'public_verification_unavailable'}
        from bs4 import BeautifulSoup

        text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
        first_words = BeautifulSoup(article.body or '', 'html.parser').get_text(' ', strip=True)[:100]
        if current.get('body', '').strip() != (article.body or '').strip():
            return {'status': 'held', 'reason': 'published_source_does_not_match'}
        if first_words and first_words not in text:
            return {'status': 'held', 'reason': 'public_content_does_not_match'}
        return {
            'status': 'published',
            'public_status': 200,
            'url': url,
            'source_hash': current.get('source_hash'),
        }
    except Exception:
        return {'status': 'held', 'reason': 'public_verification_unavailable'}


def _resolve_autopilot_parent_after_publication(db, site, article):
    """Close a parent only after a read-only reconciliation proves publication."""

    children = db.scalars(select(Job).where(
        Job.site_id == site.id,
        Job.kind == 'publish',
    )).all()
    for child in children:
        payload = child.payload if isinstance(child.payload, dict) else {}
        if payload.get('article_id') != article.id:
            continue
        parent_id = payload.get('content_autopilot_parent_job_id')
        if not isinstance(parent_id, str):
            continue
        parent = db.get(Job, parent_id)
        if parent is None or parent.site_id != site.id:
            continue
        child.status = 'complete'
        child.result = {'status': 'published', 'verified': True, 'reconciled': True}
        child.lease_until = None
        child.updated_at = now()
        previous = parent.result if isinstance(parent.result, dict) else {}
        stages = previous.get('stages') if isinstance(previous.get('stages'), list) else []
        updated_stages = []
        for stage in stages:
            if isinstance(stage, dict) and stage.get('name') == 'publication':
                updated_stages.append({**stage, 'status': 'published', 'verified': True, 'blockers': []})
            else:
                updated_stages.append(stage)
        parent.status = 'complete'
        parent.result = {
            **previous,
            'status': 'published',
            'complete': True,
            'article_id': article.id,
            'stages': updated_stages[:6],
            'blockers': [],
            'next_action': 'monitor_published_article',
        }
        parent.lease_until = None
        parent.updated_at = now()
        break


def _reconciliation_result(job, publication, *, status, complete=False, reason=None, **details):
    result = {
        'workflow': 'reconcile_publication',
        'job_id': job.id,
        'publication_id': publication.id,
        'status': status,
        'complete': complete,
        'operation_key_preserved': True,
        'next_action': 'monitor_published_article' if status == 'published' else (
            'resume_publication' if status == 'draft_reconciled' else 'review_publication_reconciliation'
        ),
    }
    if reason:
        result['reason'] = reason
    for key, value in details.items():
        if key in {'article_id', 'remote_id', 'public_status', 'url', 'source_hash'} and value is not None:
            result[key] = value
    return result


def _finish_publication_reconciliation(db, site, job, result):
    job.result = result
    job.updated_at = now()
    event_data = {
        'job_id': job.id,
        'publication_id': result.get('publication_id'),
        'status': result.get('status'),
        'reason': result.get('reason'),
    }
    event(db, site, 'publication_reconciliation_finished', 'Publication reconciliation finished', event_data)
    db.commit()
    return result


async def reconcile_publication(db, site, job):
    """Read and verify an uncertain publication without issuing a write."""

    payload = job.payload if isinstance(job.payload, dict) else {}
    publication_id = payload.get('publication_id')
    publication = db.scalar(select(Publication).where(
        Publication.site_id == site.id,
        Publication.id == publication_id,
    )) if isinstance(publication_id, str) else None
    if publication is None:
        raise ValueError('Publication is not available in this site')

    if publication.status == 'published':
        return _finish_publication_reconciliation(
            db,
            site,
            job,
            _reconciliation_result(job, publication, status='already_resolved', complete=True),
        )
    if not _publication_requires_reconciliation(publication):
        return _finish_publication_reconciliation(
            db,
            site,
            job,
            _reconciliation_result(
                job,
                publication,
                status='held',
                reason='publication_is_not_uncertain',
            ),
        )

    article = db.scalar(select(Article).where(
        Article.site_id == site.id,
        Article.id == publication.article_id,
    )) if publication.article_id else None
    if article is None:
        return _finish_publication_reconciliation(
            db,
            site,
            job,
            _reconciliation_result(job, publication, status='held', reason='article_not_found'),
        )

    snapshot = publication.snapshot if isinstance(publication.snapshot, dict) else {}
    approved_article = snapshot.get('article') if isinstance(snapshot.get('article'), dict) else None
    if not _publication_article_matches_snapshot(article, approved_article):
        return _finish_publication_reconciliation(
            db,
            site,
            job,
            _reconciliation_result(
                job,
                publication,
                status='held',
                reason='article_changed_since_publication',
                article_id=article.id,
            ),
        )

    try:
        async with await client_for(db, site) as client:
            if publication.remote_id:
                current = await client.read(f'posts:{publication.remote_id}')
            else:
                current = await client.reconcile_draft(
                    _publication_article_payload(article),
                    publication.operation_key,
                )
    except ResourceNotFound:
        current = None
        reason = 'remote_record_not_found'
    except ConnectorError as exc:
        current = None
        reason = 'reconciliation_unavailable' if exc.transport_error else 'no_unique_remote_match'
    except Exception:
        current = None
        reason = 'reconciliation_unavailable'

    if not isinstance(current, dict):
        publication.result = {
            'status': 'held',
            'reason': reason,
            'operation_key_preserved': True,
        }
        publication.updated_at = now()
        db.commit()
        return _finish_publication_reconciliation(
            db,
            site,
            job,
            _reconciliation_result(job, publication, status='held', reason=reason, article_id=article.id),
        )

    expected = snapshot.get('draft') if isinstance(snapshot.get('draft'), dict) else None
    if not _publication_remote_matches(client, current, article, expected):
        publication.result = {
            'status': 'held',
            'reason': 'snapshot_mismatch',
            'operation_key_preserved': True,
        }
        publication.updated_at = now()
        db.commit()
        return _finish_publication_reconciliation(
            db,
            site,
            job,
            _reconciliation_result(job, publication, status='held', reason='snapshot_mismatch', article_id=article.id),
        )

    remote_id = current.get('id')
    if remote_id is None and isinstance(current.get('resource_key'), str) and ':' in current['resource_key']:
        remote_id = current['resource_key'].split(':', 1)[1]
    if remote_id is not None:
        publication.remote_id = article.remote_id = str(remote_id)
    publication.snapshot = {**snapshot, 'draft': current}

    verification = await _verify_reconciled_publication(site, job, article, current)
    if verification.get('status') == 'published':
        article.status = 'published'
        publication.status = 'published'
        publication.result = {
            **verification,
            'reconciled': True,
        }
        publication.updated_at = article.updated_at = now()
        page = store_page(db, site, current)
        page.managed = True
        page.enrolled = True
        _resolve_autopilot_parent_after_publication(db, site, article)
        db.commit()
        return _finish_publication_reconciliation(
            db,
            site,
            job,
            _reconciliation_result(
                job,
                publication,
                status='published',
                complete=True,
                article_id=article.id,
                remote_id=publication.remote_id,
                public_status=verification.get('public_status'),
                url=verification.get('url'),
                source_hash=verification.get('source_hash'),
            ),
        )

    if verification.get('status') == 'draft' and isinstance(article.checks, dict) and article.checks.get('passed') is True:
        # The remote draft is now known and source-locked. Move only the local
        # ledger to a resumable state; a separate explicit publish request is
        # still required for the next remote write.
        article.status = 'checked'
        publication.status = 'preparing'
        publication.snapshot = {**publication.snapshot, 'reconciliation_job_id': job.id}
        publication.result = {
            'status': 'draft_reconciled',
            'next_action': 'resume_publication',
            'reconciled': True,
        }
        publication.updated_at = article.updated_at = now()
        db.commit()
        return _finish_publication_reconciliation(
            db,
            site,
            job,
            _reconciliation_result(
                job,
                publication,
                status='draft_reconciled',
                article_id=article.id,
                remote_id=publication.remote_id,
            ),
        )

    reason = verification.get('reason', 'remote_state_not_publishable')
    publication.result = {
        'status': 'held',
        'reason': reason,
        'operation_key_preserved': True,
    }
    publication.updated_at = now()
    db.commit()
    return _finish_publication_reconciliation(
        db,
        site,
        job,
        _reconciliation_result(job, publication, status='held', reason=reason, article_id=article.id),
    )


def publication_target(site, article, source=None):
    """Resolve a new post's policy target without treating a draft preview as its permalink.

    WordPress edit-context responses expose the sample permalink and unique
    generated slug. Unknown templates or off-site targets must stay in draft.
    """
    if source is None:
        slug = article.slug
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError('Publication target is unknown; select an article slug')
        url = site.origin.rstrip('/') + '/' + slug.lstrip('/')
    else:
        raw = source.get('raw') if isinstance(source.get('raw'), dict) else {}
        template = raw.get('permalink_template')
        url = template if template else source.get('url')
        if isinstance(url, str) and '%postname%' in url:
            slug = raw.get('generated_slug')
            if not isinstance(slug, str) or not slug.strip():
                raise ValueError('Publication permalink slug is unknown; keep the article in draft')
            url = url.replace('%postname%', slug)
    if not isinstance(url, str) or not url.strip() or re.search(r'%[a-z_]+%', url, re.I):
        raise ValueError('Publication permalink is unknown; keep the article in draft')
    try:
        parsed, origin = urlsplit(url), urlsplit(site.origin)
        same_origin = (parsed.scheme, parsed.hostname, parsed.port or 443) == (origin.scheme, origin.hostname, origin.port or 443)
    except ValueError as exc:
        raise ValueError('Publication permalink is invalid') from exc
    if not same_origin or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Publication permalink is outside the connected site')
    # New, platform-managed articles are enrolled by policy; imported/existing
    # content must take the separate explicit-enrollment refresh workflow.
    target = {'url': url, 'enrolled': article.managed}
    if article.id:
        target['article_id'] = article.id
    return target


async def checked_article(db, site, article, *, exclude_page_id=None):
    from app.authors import apply_author_check, refresh_authors, unavailable
    from app.intelligence.content import check_article
    observation = await refresh_authors(db, site) if article.author_id else unavailable('missing_author')
    pages = [record(p) for p in db.scalars(select(Page).where(Page.site_id == site.id))
             if p.id != exclude_page_id]
    return apply_author_check(check_article(record(article), site.facts, pages), article.author_id, observation)


async def publish(db, site, job):
    from app.authors import AuthorVerificationError, require_current_author
    from app.intelligence.content import check_article
    article = scoped(db, Article, job.payload.get('article_id'), site)
    if isinstance(article.brief,dict) and article.brief.get('purpose') == 'refresh_existing':
        return await apply_refresh(db, site, job, article)
    # Report emergency/policy holds before resolving a potentially incomplete
    # article target; neither path is allowed to open the connector.
    authorize(db, site, 'publish')
    policy = authorize(db, site, 'publish', publication_target(site, article))
    op_key = f'publish:{site.id}:{article.id}'
    pub = publication(db, site, op_key, policy, article=article)
    if pub.status == 'published':
        return pub.result
    if pub.status in ('failed','rolled_back','ambiguous'):
        raise ValueError('This publication requires reconciliation before another write')
    if article.scheduled_at and article.scheduled_at > now():
        raise ValueError('Publication is scheduled for a future time')
    from datetime import timezone
    from zoneinfo import ZoneInfo
    local_now = now().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(site.timezone))
    week_start = (local_now - timedelta(days=local_now.weekday())).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    count = db.scalar(select(func.count()).select_from(Publication).where(Publication.site_id == site.id,
                      Publication.article_id.is_not(None), Publication.status == 'published', Publication.created_at >= week_start))
    if count >= policy.settings.get('posts_per_week',2):
        raise ValueError('Weekly publication limit reached')
    checks = await checked_article(db, site, article)
    article.checks = checks
    if not checks['passed']:
        article.status = 'review_needed'
        db.commit()
        raise ValueError('Editorial checks failed: ' + ', '.join(checks['blockers']))
    author = article.author_id or policy.settings.get('author_id')
    if not author:
        raise ValueError('Select a verified WordPress author before publishing')
    if pub.snapshot.get('article'):
        captured=pub.snapshot['article']
        if any(captured.get(key)!=getattr(article,key) for key in ('title','body','slug','author_id')):
            raise ValueError('The article changed during publication; reconcile the earlier attempt first')
    pub.snapshot = {**pub.snapshot,'article':record(article),'authorization':{'type':'policy','version':policy.version}}
    article.status, pub.status = 'publishing','publishing'
    db.commit()
    async with await client_for(db, site) as client:
        try:
            authorize(db, site, 'publish', publication_target(site, article))
            if pub.remote_id:
                draft = await client.read('posts:' + pub.remote_id)
            else:
                draft_payload={'title':article.title,'body':article.body,'slug':article.slug,'author_id':author}
                if pub.snapshot.get('create_started'):
                    draft=await client.reconcile_draft(draft_payload,op_key)
                else:
                    await require_current_author(client, author)
                    authorize(db, site, 'publish', publication_target(site, article))
                    pub.snapshot={**pub.snapshot,'create_started':iso(now())}
                    db.commit()
                    draft = await client.create_draft(draft_payload, op_key)
                pub.remote_id = str(draft['id'])
                article.remote_id = pub.remote_id
                pub.snapshot = {**pub.snapshot,'draft':draft}
                db.commit()
            authorize(db, site, 'publish', publication_target(site, article, draft))
            captured_draft=pub.snapshot.get('draft')
            if not captured_draft or not client.matches_snapshot(draft,captured_draft,ignore_status=True):
                raise SourceConflict(captured_draft.get('source_hash') if captured_draft else 'missing_snapshot',draft['source_hash'],resource_key=draft['resource_key'])
            if draft.get('status') not in ('draft','publish'):
                raise SourceConflict('draft_or_publish',draft.get('status'),resource_key=draft['resource_key'])
            if draft.get('status') != 'publish':
                await require_current_author(client, author)
                authorize(db, site, 'publish', publication_target(site, article, draft))
                await client.publish(
                    pub.remote_id,
                    expected_hash=draft['source_hash'],
                    operation_key=pub.operation_key,
                )
            article.status, pub.status = 'verifying','verifying'
            db.commit()
            source = await client.read('posts:' + pub.remote_id)
            response = await fetch(source['url'])
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response['html'],'html.parser')
            text = soup.get_text(' ',strip=True)
            first_words = BeautifulSoup(article.body,'html.parser').get_text(' ',strip=True)[:100]
            if response['status_code'] != 200 or source.get('status') != 'publish' or source.get('body','').strip() != article.body.strip() or (first_words and first_words not in text):
                raise ValueError('Public/source verification did not match the approved article')
            page = store_page(db, site, source)
            page.managed, page.enrolled = True, True
            article.status, pub.status = 'published','published'
            pub.result = {'status':'published','url':source['url'],'public_status':response['status_code'],'source_hash':source['source_hash'],'verified_at':iso(now()),
                          'evidence':capture_html(site,job,source['url'],response['html'],'published_verification')}
            pub.updated_at = article.updated_at = now()
            event(db, site, 'article_published', f'Published and verified: {article.title}', {'article_id':article.id,'publication_id':pub.id,**pub.result})
            return pub.result
        except AuthorVerificationError:
            article.status, pub.status = 'review_needed', 'failed'
            pub.result = {'status': 'blocked', 'blocker': 'author_not_verified'}
            pub.updated_at = now()
            db.commit()
            raise ValueError('Publication stopped because the author is no longer verified') from None
        except Exception as exc:
            article.status = 'failed'
            # An ambiguous/transport timeout means the remote write may have
            # succeeded even though this worker did not receive a definitive
            # result.  Do not compensate from a possibly stale read here: a
            # read can observe the successful publication and an automatic
            # restore would silently undo it.  Leave the operation ambiguous
            # for the read-only reconciliation workflow to resolve.
            outcome_unknown = (
                isinstance(exc, (AmbiguousOutcome, TimeoutError))
                or (isinstance(exc, ConnectorError) and exc.transport_error)
            )
            pub.status = 'ambiguous' if pub.remote_id is None or outcome_unknown else 'failed'
            if (
                pub.remote_id
                and pub.snapshot.get('draft')
                and not isinstance(exc, SourceConflict)
                and not outcome_unknown
            ):
                try:
                    current = await client.read('posts:' + pub.remote_id)
                    # Do not revert someone else's changes after a timeout.
                    if not client.matches_snapshot(current,pub.snapshot['draft'],ignore_status=True):
                        raise ValueError('Remote article changed; rollback needs review')
                    restored = await client.restore(
                        'posts:' + pub.remote_id,
                        pub.snapshot['draft'],
                        current['source_hash'],
                        operation_key=pub.operation_key,
                    )
                    if restored.get('status') != 'draft':
                        raise ValueError('Rollback did not restore draft status')
                    article.status, pub.status = 'rolled_back','rolled_back'
                except Exception:
                    incident(db, site, f'rollback:{pub.id}', 'Publication rollback needs attention', details={'publication_id':pub.id})
            if isinstance(exc,SourceConflict):
                article.status='review_needed'
                incident(db,site,f'publication_conflict:{pub.id}','External edit preserved; publication paused',details={'publication_id':pub.id})
            pub.result = {'status':pub.status,'error_type':type(exc).__name__}
            pub.updated_at = now()
            db.commit()
            raise


async def apply_refresh(db, site, job, article):
    """Apply an enrolled existing-page refresh as a guarded in-place edit.

    Refreshes do not create a new WordPress post.  They use the same snapshot,
    source-lock, public verification, and rollback discipline as publication,
    but authorize the narrower ``refresh`` policy action and preserve the
    target page's resource identity.
    """
    from app.intelligence.audit import audit_page
    from app.intelligence.content import check_article

    brief = article.brief if isinstance(article.brief, dict) else {}
    page_id = brief.get('refresh_of_page_id')
    if not isinstance(page_id, str) or not page_id:
        raise ValueError('Refresh draft has no enrolled page target')
    page = scoped(db, Page, page_id, site)
    if not page.enrolled:
        raise ValueError('Refresh target is no longer enrolled')
    if page.resource_type not in ('posts', 'pages'):
        raise ValueError('Refresh format is unsupported for this resource')

    policy = authorize(db, site, 'refresh', page)
    op_key = f'refresh:{site.id}:{article.id}'
    pub = publication(db, site, op_key, policy, article=article)
    if pub.status == 'published':
        return pub.result
    if pub.status in ('failed', 'rolled_back', 'ambiguous'):
        raise ValueError('This refresh requires reconciliation before another write')

    expected_hash = brief.get('source_hash')
    if not isinstance(expected_hash, str) or not expected_hash:
        raise ValueError('Refresh draft has no source snapshot')
    checks = await checked_article(db, site, article, exclude_page_id=page.id)
    article.checks = checks
    if not checks['passed']:
        article.status = 'review_needed'
        db.commit()
        raise ValueError('Refresh editorial checks failed: ' + ', '.join(checks['blockers']))

    kind = 'woocommerce' if page.resource_type in ('products', 'categories', 'product_categories') else 'wordpress'
    async with await client_for(db, site, kind) as client:
        before = await client.read(page.resource_key)
        if before.get('source_hash') != expected_hash or page.source_hash != expected_hash:
            article.status = 'review_needed'
            pub.status = 'failed'
            pub.result = {
                'status': 'source_conflict',
                'expected_source_hash': expected_hash,
                'stored_source_hash': page.source_hash,
                'remote_source_hash': before.get('source_hash'),
            }
            pub.updated_at = now()
            incident(db, site, f'refresh_conflict:{article.id}', 'Enrolled page changed before refresh',
                     details={'article_id': article.id, 'page_id': page.id, 'expected_hash': expected_hash,
                              'remote_hash': before.get('source_hash'), 'stored_hash': page.source_hash})
            db.commit()
            raise ValueError('Source changed since this refresh was evaluated')
        public_before = await fetch(page.url)
        if public_before.get('status_code') != 200:
            raise ValueError('Refresh target is not publicly available')
        protected_before = protected_html(public_before.get('html', ''))
        # Editorial body text and headings are intentionally changed by this
        # workflow; layout, styles, canonical, and indexing directives are not.
        protected_before = {key: value for key, value in protected_before.items()
                            if key not in {'body_text', 'headings'}}
        pub.remote_id = str(before.get('id') or page.resource_key.split(':', 1)[-1])
        pub.snapshot = {
            **pub.snapshot,
            'source': before,
            'evidence': capture_html(site, job, page.url, public_before.get('html', ''), 'before_refresh'),
            'protected': protected_before,
            'authorization': {'type': 'policy', 'version': policy.version},
        }
        pub.status, article.status = 'publishing', 'publishing'
        db.commit()
        try:
            authorize(db, site, 'refresh', page)
            desired = {'title': article.title, 'body': article.body}
            after = await client.update(
                page.resource_key,
                desired,
                before['source_hash'],
                operation_key=pub.operation_key,
            )
            public_after = await fetch(page.url)
            if public_after.get('status_code') != 200:
                raise ValueError('Refreshed page could not be publicly verified')
            protected_after = protected_html(public_after.get('html', ''))
            protected_after = {key: value for key, value in protected_after.items()
                               if key not in {'body_text', 'headings'}}
            if after.get('title', '').strip() != article.title.strip() or after.get('body', '').strip() != article.body.strip():
                raise ValueError('Refresh source verification did not match the approved article')
            if protected_before != protected_after:
                raise ValueError('Refresh verification detected layout, style, canonical, or robots drift')
            observation = audit_page(page.url, public_after.get('html', ''), after)
            evidence = capture_html(site, job, page.url, public_after.get('html', ''), 'after_refresh')
            observation['signals'] = {**observation['signals'], 'observation_type': 'source_html',
                                      'observed_at': iso(now()), 'evidence': evidence}
            for finding in observation['findings']:
                finding['details'] = {**finding.get('details', {}), 'evidence': evidence}
            page.source, page.source_hash = after, after['source_hash']
            page.last_seen_at = now()
            upsert_observation(db, site, page, observation)
            article.status = 'refreshed'
            pub.status = 'published'
            pub.result = {'status': 'refreshed', 'url': page.url, 'public_status': 200,
                          'source_hash': after['source_hash'], 'verified_at': iso(now()),
                          'evidence': evidence}
            pub.updated_at = article.updated_at = now()
            event(db, site, 'article_refreshed', f'Enrolled article refreshed and verified: {article.title}',
                  {'article_id': article.id, 'page_id': page.id, 'publication_id': pub.id, **pub.result})
            return pub.result
        except Exception as exc:
            article.status = 'failed'
            pub.status = 'failed'
            try:
                current = await client.read(page.resource_key)
                desired_matches = (current.get('title', '').strip() == article.title.strip()
                                   and current.get('body', '').strip() == article.body.strip())
                if current.get('source_hash') != before.get('source_hash') and not desired_matches:
                    raise ValueError('Remote page changed outside the approved refresh')
                if current.get('source_hash') != before.get('source_hash'):
                    restored = await client.restore(
                        page.resource_key,
                        before,
                        current['source_hash'],
                        operation_key=pub.operation_key,
                    )
                    if restored.get('source_hash') != before.get('source_hash'):
                        raise ValueError('Refresh rollback source verification failed')
                article.status, pub.status = 'review_needed', 'rolled_back'
            except Exception:
                incident(db, site, f'rollback:{pub.id}', 'Refresh rollback needs attention',
                         details={'publication_id': pub.id, 'article_id': article.id})
            pub.result = {'status': pub.status, 'error_type': type(exc).__name__}
            pub.updated_at = now()
            db.commit()
            raise


async def candidate(db, site, job):
    from app.intelligence.content import check_metadata
    row = scoped(db, Candidate, job.payload.get('candidate_id'), site)
    page = scoped(db, Page, row.page_id, site)
    if row.status == 'applied':
        return {'status':'applied','candidate_id':row.id,'replayed':True}
    if row.status != 'approved':
        raise ValueError('Candidate must be approved before execution')
    readiness = metadata_candidate_readiness(db, site, page, row.field)
    blocked_reasons = candidate_execution_blockers(db, site, row, page)
    if blocked_reasons:
        row.status = 'review_needed'
        row.details = {
            **apply_metadata_readiness(row.details, readiness),
            'review_only_reasons': list(dict.fromkeys([
                *review_only_reasons(row),
                *([*readiness['blockers']] if readiness is not None else []),
            ])),
            'execution_attempt': {
                'status': 'blocked',
                'reasons': blocked_reasons,
                'at': iso(now()),
            },
        }
        db.commit()
        raise ValueError('Candidate is review-only: ' + ', '.join(blocked_reasons))
    if row.field not in ('seo_title','meta_description'):
        raise ValueError('This candidate type needs an implemented writer')
    if page.resource_type == 'discovered_page':
        raise ValueError('Page has no verified writable resource mapping')
    policy = authorize(db, site, 'metadata', page)
    # Complete-meaning check is independent from API readiness or character length.
    checks = check_metadata(row.field, row.after_value)
    if not checks.get('passed'):
        row.status = 'review_needed'
        db.commit()
        raise ValueError('Metadata editorial checks failed')
    kind = 'woocommerce' if page.resource_type in ('products','categories','product_categories') else 'wordpress'
    pub = publication(db, site, f'candidate:{site.id}:{row.id}', policy, candidate=row)
    if pub.status in ('failed','rolled_back','ambiguous'):
        raise ValueError('Previous mutation requires reconciliation')
    async with await client_for(db, site, kind) as client:
        before = await client.read(page.resource_key)
        if before['source_hash'] != row.source_hash:
            row.status = 'stale'
            pub.status = 'failed'
            pub.result = {
                'status': 'stale',
                'reason': 'source_conflict',
                'expected_source_hash': row.source_hash,
                'remote_source_hash': before.get('source_hash'),
            }
            pub.updated_at = now()
            incident(
                db,
                site,
                f'candidate_conflict:{row.id}',
                'Approved metadata change became stale before writing',
                kind='conflict',
                severity='medium',
                details={
                    'candidate_id': row.id,
                    'publication_id': pub.id,
                    'expected_source_hash': row.source_hash,
                    'remote_source_hash': before.get('source_hash'),
                },
            )
            db.commit()
            raise ValueError('Source changed since this candidate was generated')
        before_public = await fetch(page.url)
        if before_public['status_code'] != 200:
            raise ValueError('Public page is not available')
        from app.intelligence.audit import audit_page
        before_signals = audit_page(page.url,before_public['html'],before)['signals']
        protected_before = protected_html(before_public['html'])
        pub.snapshot = {'source':before,'signals':before_signals,'evidence':capture_html(site,job,page.url,before_public['html'],'before_metadata'),
                        'authorization':row.details.get('authorization',{'type':'policy','version':policy.version})}
        pub.status, row.status, row.policy_version = 'publishing','executing',policy.version
        db.commit()
        try:
            authorize(db, site, 'metadata', page)
            field_name='title' if row.field=='seo_title' else 'description'
            update_kwargs = {'operation_key': pub.operation_key} if kind == 'wordpress' else {}
            after = await client.update(
                page.resource_key,
                {'seo': {field_name: row.after_value}},
                before['source_hash'],
                **update_kwargs,
            )
            response = await fetch(page.url)
            signals = audit_page(page.url,response['html'],after)['signals']
            signals['evidence']=capture_html(site,job,page.url,response['html'],'after_metadata')
            actual = signals.get('title' if row.field == 'seo_title' else 'meta_description')
            protected_after = protected_html(response['html'])
            drift = [k for k in protected_before if protected_before[k] != protected_after[k]]
            if response['status_code'] != 200 or actual != row.after_value or drift:
                raise ValueError('Rendered verification failed')
            page.source, page.source_hash, page.signals = after,after['source_hash'],signals
            # Rebase unrelated candidates against the verified post-write
            # source. They remain independently actionable; only candidates
            # whose requested value is already present become historical
            # applied records. This prevents one approved edit from deleting
            # or incorrectly resolving its siblings.
            for sibling in db.scalars(select(Candidate).where(
                    Candidate.site_id == site.id, Candidate.page_id == page.id,
                    Candidate.id != row.id, Candidate.status.in_(['pending','approved']))):
                current_value = candidate_value(after, sibling.field)
                if current_value == sibling.after_value:
                    sibling.status = 'applied'
                else:
                    sibling.before_value = current_value
                    sibling.source_hash = after['source_hash']
            row.status, pub.status = 'applied','applied'
            pub.result = {'status':'applied','public_status':200,'protected_drift':drift,'verified_value':actual,'source_hash':after['source_hash']}
            pub.updated_at = now()
            event(db, site, 'candidate_applied', f'{row.field.replace("_"," ")} updated: {page.title}', {'candidate_id':row.id,'publication_id':pub.id})
            return pub.result
        except Exception as exc:
            pub.status, row.status = 'failed','failed'
            try:
                current = await client.read(page.resource_key)
                if candidate_value(current, row.field) != row.after_value:
                    raise ValueError('Cannot safely revert an uncertain or externally edited value')
                # A metadata operation owns only its selected SEO field.  The
                # full source snapshot also contains native editorial fields;
                # replaying it here could overwrite an unrelated external edit
                # made after our write.  Use the connector's guarded metadata
                # writer so the current source remains the concurrency boundary.
                field_name = 'title' if row.field == 'seo_title' else 'description'
                restore_kwargs = {'operation_key': pub.operation_key} if kind == 'wordpress' else {}
                restored = await client.update(
                    page.resource_key,
                    {'seo': {field_name: row.before_value or ''}},
                    current['source_hash'],
                    **restore_kwargs,
                )
                if candidate_value(restored, row.field) != (row.before_value or ''):
                    raise ValueError('Rollback metadata verification failed')
                restored_public = await fetch(page.url)
                if restored_public['status_code'] != 200 or protected_html(restored_public['html']) != protected_before:
                    raise ValueError('Rollback public verification failed')
                pub.status, row.status = 'rolled_back','rolled_back'
            except Exception:
                incident(db, site, f'rollback:{pub.id}', 'Metadata rollback needs attention', details={'publication_id':pub.id})
            pub.result = {'status':pub.status,'error_type':type(exc).__name__}
            db.commit()
            raise


async def visibility(db, site, job):
    from app.intelligence.visibility import (
        _bounded_ai_questions,
        collect,
        paid_visibility_preflight_error,
    )
    kind = job.payload.get('kind', 'gsc')
    connection_kind = 'ai' if kind == 'ai_sample' else kind
    secret, config = credentials(db, site.id, connection_kind)
    policy = current_policy(db, site.id)
    config = {**(policy.settings if policy else {}), **config}
    config.setdefault('site_url',site.origin)
    requested_mode = job.payload.get('mode') if isinstance(job.payload, dict) else None
    if requested_mode is not None:
        if kind != 'dataforseo' or requested_mode != 'competitors':
            raise ValueError('Unsupported visibility mode')
        policy_competitors = (policy.settings if policy else {}).get('competitors', [])
        if not isinstance(policy_competitors, list) or not policy_competitors:
            raise ValueError('Competitor observations require policy competitors')
        config['mode'] = 'competitors'
        config['competitors'] = list(policy_competitors[:3])
    if kind == 'pagespeed':
        # A PageSpeed connection is useful immediately after onboarding. The
        # owner may configure a representative template URL, otherwise the
        # public site origin is the conservative default sample.
        config.setdefault('url', site.origin)
    config.setdefault('keywords',config.get('tracked_keywords',[]))
    config.setdefault('questions',config.get('tracked_questions',[]))
    paid = kind in ('dataforseo','ai_sample')
    reservation_config = config
    if kind == 'ai_sample':
        raw_questions = config.get('questions')
        questions = _bounded_ai_questions(
            raw_questions,
            singular=isinstance(raw_questions, str),
        )
        if questions is None:
            raise ValueError('AI visibility collection requires 1-20 non-empty tracked questions')
        # Normalize the validated list before both reservation and collection
        # so duplicate/whitespace-only inputs cannot change the reserved count.
        config['questions'] = questions
        request_format = str(config.get('request_format') or '').strip().casefold()
        if request_format in {'openai_responses_web_search', 'responses_web_search', 'openai_responses'}:
            question_count = len(questions)
            reservation_config = dict(config)
            for cost_key in ('estimated_cost_cents', 'max_cost_cents'):
                value = reservation_config.get(cost_key)
                if isinstance(value, int) and not isinstance(value, bool):
                    reservation_config[cost_key] = value * question_count
    if paid:
        preflight_error = paid_visibility_preflight_error(kind, secret, config)
        if preflight_error:
            raise ValueError(preflight_error)
    reservation = paid_reservation(db, site, 'visibility:' + job.id, reservation_config) if paid else None
    result = await collect(kind,secret,config)
    if result.get('status') == 'error' or result.get('error'):
        if reservation and result.get('cost_basis') == 'provider_actual':
            reconcile_provider_cost(db,site,reservation,result)
        raise ValueError('Visibility collection needs attention: '+result.get('error',{}).get('code','provider_error'))
    if reservation:
        reconcile_provider_cost(db,site,reservation,result)
    data=result['data'] if isinstance(result['data'],dict) else {'observations':result['data']}
    data={**data,'_collection':{'cost_basis':result.get('cost_basis','unknown'),'usage':result.get('usage'),
                              'provider_metadata':result.get('metadata',{}),'observed_at':result.get('observed_at')}}
    db.add(Measurement(site_id=site.id,kind=result['kind'],source=result['source'],data=data,observed_at=now()))
    connection = find_connection(db,site.id,connection_kind)
    connection.status, connection.checked_at = 'connected',now()
    event(db,site,'visibility_collected',f'{kind} observations updated')
    return {k:v for k,v in result.items() if k != 'data'}


async def rollback(db, site, job):
    article = scoped(db,Article,job.payload.get('article_id'),site)
    pub = db.scalar(select(Publication).where(Publication.site_id == site.id,Publication.article_id == article.id,Publication.status == 'published').order_by(Publication.created_at.desc()))
    if isinstance(article.brief, dict) and article.brief.get('purpose') == 'refresh_existing':
        if pub is None or not pub.snapshot.get('source') or not pub.result.get('source_hash'):
            raise ValueError('There is no verified refresh snapshot to restore')
        page = scoped(db, Page, article.brief.get('refresh_of_page_id'), site)
        authorize(db, site, 'refresh', page)
        async with await client_for(db, site, 'wordpress') as client:
            current = await client.read(page.resource_key)
            if current.get('source_hash') != pub.result.get('source_hash'):
                raise ValueError('Refreshed content has changed; rollback requires review')
            restored = await client.restore(
                page.resource_key,
                pub.snapshot['source'],
                current['source_hash'],
                operation_key=pub.operation_key,
            )
            if restored.get('source_hash') != pub.snapshot['source'].get('source_hash'):
                raise ValueError('Refresh rollback verification failed')
        page.source, page.source_hash = restored, restored['source_hash']
        page.last_seen_at = now()
        article.status, pub.status = 'rolled_back', 'rolled_back'
        pub.updated_at = article.updated_at = now()
        event(db, site, 'refresh_rolled_back', 'Enrolled article refresh was rolled back',
              {'publication_id': pub.id, 'article_id': article.id, 'page_id': page.id})
        return {'status': 'rolled_back'}
    authorize(db,site,'publish')
    if pub is None or not pub.remote_id or 'draft' not in pub.snapshot:
        raise ValueError('There is no verified publication snapshot to restore')
    async with await client_for(db,site) as client:
        current = await client.read('posts:' + pub.remote_id)
        if current['source_hash'] != pub.result.get('source_hash'):
            raise ValueError('Published content has changed; rollback requires review')
        restored = await client.restore(
            'posts:' + pub.remote_id,
            pub.snapshot['draft'],
            current['source_hash'],
            operation_key=pub.operation_key,
        )
        if restored.get('status') != 'draft':
            raise ValueError('Rollback verification failed')
    pub.status,article.status = 'rolled_back','rolled_back'
    pub.updated_at = article.updated_at = now()
    event(db,site,'publication_rolled_back','Article restored to draft',{'publication_id':pub.id})
    return {'status':'rolled_back'}


async def refresh(db,site,job):
    from datetime import timezone
    from zoneinfo import ZoneInfo
    policy = current_policy(db,site.id)
    limit = policy.settings.get('refreshes_per_week',1) if policy else 1
    if not isinstance(limit,int) or isinstance(limit,bool) or limit <= 0:
        return {'evaluated':0,'article_ids':[],'status':'disabled_by_policy'}
    local_now = now().replace(tzinfo=timezone.utc).astimezone(ZoneInfo(site.timezone))
    week_key = (local_now - timedelta(days=local_now.weekday())).date().isoformat()
    existing = db.scalars(select(Article).where(Article.site_id == site.id)).all()
    article_ids=[]
    planned_count = 0
    evaluated = 0
    page_offset = 0
    # Do not let already-evaluated pages consume this week's refresh quota.
    # Page inventory can be large, so scan bounded batches until the requested
    # number of new refresh evaluations is found rather than loading the full
    # site or limiting the first query before duplicate filtering.
    batch_size = max(50, min(500, limit * 4))
    while len(article_ids) < limit:
        pages = db.scalars(
            select(Page)
            .where(
                Page.site_id == site.id,
                Page.enrolled.is_(True),
                Page.resource_type.in_(['posts','pages','post','page']),
            )
            .order_by(Page.last_seen_at.asc().nullsfirst(), Page.id.asc())
            .offset(page_offset)
            .limit(batch_size)
        ).all()
        if not pages:
            break
        page_offset += len(pages)
        for page in pages:
            evaluated += 1
            refresh_key=f'{page.id}:{week_key}'
            if any(isinstance(article.brief,dict) and article.brief.get('refresh_key') == refresh_key for article in existing):
                continue
            remote_id = page.resource_key.split(':', 1)[1] if ':' in page.resource_key else ''
            managed_source = next(
                (
                    article for article in existing
                    if article.remote_id == remote_id and article.managed
                ),
                None,
            ) if remote_id else None
            auto_maintenance = bool(
                managed_source is not None
                and not site.paused
                and not global_controls(db)['global_pause']
                and policy is not None
                and policy.settings.get('enabled') is True
                and 'refresh' in policy.settings.get('allowed_actions', [])
                and 'publish' in policy.settings.get('allowed_actions', [])
            )
            source = page.source if isinstance(page.source,dict) else {}
            source_body = source.get('body','') if isinstance(source.get('body',''),str) else ''
            source_title = source.get('title','') if isinstance(source.get('title',''),str) else page.title
            source_author = source.get('author') or source.get('author_id') or (
                managed_source.author_id if managed_source is not None else None
            )
            brief={
                'purpose':'refresh_existing',
                'refresh_key':refresh_key,
                'refresh_of_page_id':page.id,
                'resource_key':page.resource_key,
                'source_hash':page.source_hash,
                'source_url':page.url,
                'research_required':True,
                'managed_source_article_id': managed_source.id if managed_source is not None else None,
                'automatic_maintenance': auto_maintenance,
                'sources':[{'url':page.url,'source_kind':'existing_page'}],
                'research':{'complete':False,'blockers':['refresh_research_required'],'sources':[],'research_notes':[]},
            }
            article=Article(site_id=site.id,title=source_title or page.title or 'Untitled enrolled article',
                            slug=str(source.get('slug') or ''),body=source_body,
                            status='planned' if auto_maintenance else 'review_needed',brief=brief,
                            checks={'passed':False,'blockers':[] if auto_maintenance else ['refresh_research_required'],'warnings':[]},
                            sources=[{'url':page.url,'source_kind':'existing_page','status':'reference'}],
                            author_id=str(source_author) if source_author else (policy.settings.get('author_id') if policy else None),
                            managed=False,updated_at=now())
            db.add(article)
            db.flush()
            existing.append(article)
            article_ids.append(article.id)
            if auto_maintenance:
                planned_count += 1
            event_kind = 'refresh_planned' if auto_maintenance else 'refresh_evaluated'
            next_action = 'research_and_editorial_review' if not auto_maintenance else 'research_generation_and_policy_verification'
            event(db,site,event_kind,
                  f'Enrolled article refresh {"planned for automatic maintenance" if auto_maintenance else "needs review"}: {page.title or page.url}',
                  {'page_id':page.id,'article_id':article.id,'next_action':next_action,'source_hash':page.source_hash})
            if len(article_ids) >= limit:
                break
    if not article_ids:
        status = 'already_evaluated' if evaluated else 'no_enrolled_pages'
    elif planned_count == len(article_ids):
        status = 'planned'
    else:
        status = 'review_needed'
    return {'evaluated':evaluated,'article_ids':article_ids,'planned_count':planned_count,'status':status}


_FULL_CYCLE_STAGE_SPECS = (
    ('availability', 'availability'),
    ('inventory', 'inventory'),
    ('public_audit', 'audit'),
    ('content_plan', 'plan'),
    ('refresh_evaluation', 'refresh'),
)
_FULL_CYCLE_AUTOPILOT_STAGE_SPECS = (
    ('availability', 'availability'),
    ('inventory', 'inventory'),
    ('public_audit', 'audit'),
    ('content_plan', 'plan'),
    ('content_autopilot', 'content_autopilot'),
    ('refresh_evaluation', 'refresh'),
)
_FULL_CYCLE_MODES = frozenset({'read_only', 'governed', 'autopilot'})
_FULL_CYCLE_MODE_ERROR = 'Unsupported full_cycle mode'


def full_cycle_mode(payload):
    """Validate and return the bounded full-cycle execution mode."""

    payload = payload if isinstance(payload, dict) else {}
    mode = payload.get('mode', 'read_only')
    if not isinstance(mode, str) or mode not in _FULL_CYCLE_MODES:
        raise ValueError(_FULL_CYCLE_MODE_ERROR)
    return mode


def _full_cycle_stage_specs(mode):
    """Return the stable stage contract for the requested full-cycle mode."""

    return _FULL_CYCLE_AUTOPILOT_STAGE_SPECS if mode == 'autopilot' else _FULL_CYCLE_STAGE_SPECS


def _full_cycle_pause_reason(db, site, mode):
    """Return the durable pause reason for a write-capable full cycle."""

    if mode != 'autopilot':
        return None
    if site.paused:
        return 'site_paused'
    if global_controls(db).get('global_pause'):
        return 'global_pause'
    return None


def _full_cycle_stage_id(site, parent_job, stage_name):
    """Return a deterministic Job id within the model's 32-character limit."""

    return hashlib.sha256(
        f'full_cycle:{site.id}:{parent_job.id}:{stage_name}'.encode('utf-8')
    ).hexdigest()[:32]


def _full_cycle_stage_payload(parent_job, stage_name):
    """Copy only bounded, read-only controls into a stage job."""

    parent_payload = parent_job.payload if isinstance(parent_job.payload, dict) else {}
    mode = full_cycle_mode(parent_payload)
    payload = {
        'full_cycle_parent_job_id': parent_job.id,
        'full_cycle_stage': stage_name,
    }
    if isinstance(parent_payload.get('max_pages'), int) and not isinstance(parent_payload.get('max_pages'), bool):
        payload['max_pages'] = max(1, min(100, parent_payload['max_pages']))
    if stage_name == 'public_audit':
        # Inventory is its own stage, and a full cycle is never allowed to
        # authorize or queue metadata execution as an audit side effect unless
        # the caller explicitly selected a policy-governed mode. The owner
        # autopilot uses the same bounded metadata gate as governed mode while
        # retaining its separate content-publication workflow.
        metadata_mode = mode in {'governed', 'autopilot'}
        payload.update({
            'skip_inventory': True,
            'suppress_automation': not metadata_mode,
        })
        if metadata_mode:
            payload['full_cycle_mode'] = 'governed'
    if mode == 'autopilot' and stage_name in {'content_plan', 'content_autopilot'}:
        # Keep the planning and publication side of this one-command workflow
        # bounded to one article. The content-autopilot handler remains the
        # sole owner of research, generation, publication, and verification.
        payload['max_articles'] = 1
    return payload


def _full_cycle_stage_job(db, site, parent_job, stage_name):
    stage_id = _full_cycle_stage_id(site, parent_job, stage_name)
    row = db.get(Job, stage_id)
    if row is not None:
        if row.site_id != site.id or row.kind != stage_name:
            raise ValueError('Full-cycle stage identity conflict')
        return row
    timestamp = now()
    row = Job(
        id=stage_id,
        site_id=site.id,
        kind=stage_name,
        status='queued',
        payload=_full_cycle_stage_payload(parent_job, stage_name),
        result={},
        idempotency_key=f'{site.id}:full-cycle:{parent_job.id}:{stage_name}',
        available_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )
    db.add(row)
    db.flush()
    return row


def _full_cycle_safe_reason(stage_name, exc=None):
    """Return a stable reason without exposing provider or credential text."""

    if stage_name == 'inventory':
        return 'wordpress_connection_verification_failed'
    if stage_name == 'public_audit':
        return 'public_audit_unavailable'
    return 'stage_unavailable'


def _full_cycle_stage_entry(stage_name, stage_job, status, result=None, reason=None):
    entry = {'name': stage_name, 'status': status, 'stage_job_id': stage_job.id}
    if result is not None:
        entry['result'] = result
    if reason is not None:
        entry['reason'] = reason
    return entry


def _full_cycle_id_list(value):
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item)[:64] for item in value[:_GOVERNED_METADATA_LIMIT] if str(item).strip()]


def _full_cycle_content_publishing(stages):
    """Project the content-autopilot result into a stable parent contract."""

    stage = next((item for item in stages if item.get('name') == 'content_autopilot'), None)
    if not isinstance(stage, dict):
        return {
            'action': 'publish',
            'status': 'not_run',
            'next_action': 'run_content_autopilot',
            'reason': 'content_autopilot_not_run',
        }

    raw = stage.get('result') if isinstance(stage.get('result'), dict) else {}
    raw_status = raw.get('status')
    status_map = {
        'published': 'published',
        'gated': 'gated',
        'needs_review': 'needs_review',
        'ambiguous': 'needs_reconciliation',
        'needs_reconciliation': 'needs_reconciliation',
        'failed': 'failed',
        'not_run': 'not_run',
    }
    status = status_map.get(raw_status)
    if status is None:
        status = 'failed' if stage.get('status') == 'failed' else 'needs_review'
    default_actions = {
        'published': 'monitor_published_article',
        'gated': 'resolve_content_autopilot_blockers',
        'needs_review': 'review_content_autopilot_result',
        'needs_reconciliation': 'reconcile_publication',
        'failed': 'review_content_autopilot_failure',
        'not_run': 'run_content_autopilot',
    }
    requested_action = raw.get('next_action')
    next_action = (
        str(requested_action)[:96]
        if isinstance(requested_action, str) and requested_action.strip()
        else default_actions[status]
    )
    blockers = _safe_reason_codes(raw.get('blockers', []))
    result = {
        'action': 'publish',
        'status': status,
        'next_action': next_action,
        'blockers': blockers,
    }
    article_id = raw.get('article_id') or stage.get('article_id')
    if isinstance(article_id, str) and article_id.strip():
        result['article_id'] = article_id[:64]
    if blockers:
        result['reason'] = blockers[0]
    return result


def _full_cycle_execution_summary(mode, stages):
    metadata_gate_blockers = (
        ['read_only_mode'] if mode == 'read_only' else []
    )
    metadata = {
        'status': 'review_gated' if mode in {'governed', 'autopilot'} else 'not_run',
        'candidate_limit': _GOVERNED_METADATA_LIMIT,
        'examined_count': 0,
        'authorized_count': 0,
        'queued_count': 0,
        'authorized_candidate_ids': [],
        'queued_job_ids': [],
        'authorized': [],
        'review_gated': [],
        'gate_blockers': metadata_gate_blockers,
    }
    if mode in {'governed', 'autopilot'}:
        audit_stage = next((stage for stage in stages if stage['name'] == 'public_audit'), None)
        raw = audit_stage.get('result', {}).get('metadata_execution') if audit_stage else None
        if isinstance(raw, dict):
            metadata['examined_count'] = _bounded_progress_int(
                raw.get('examined_count'), minimum=0, maximum=_GOVERNED_METADATA_LIMIT,
            ) or 0
            metadata['authorized_candidate_ids'] = _full_cycle_id_list(
                raw.get('authorized_candidate_ids'),
            )
            metadata['queued_job_ids'] = _full_cycle_id_list(raw.get('queued_job_ids'))
            metadata['authorized_count'] = len(metadata['authorized_candidate_ids'])
            metadata['queued_count'] = len(metadata['queued_job_ids'])
            metadata['status'] = 'queued' if metadata['queued_job_ids'] else 'review_gated'
            metadata['gate_blockers'] = _safe_reason_codes(raw.get('gate_blockers', []))
            raw_review_gated = raw.get('review_gated')
            if isinstance(raw_review_gated, list):
                for item in raw_review_gated[:_GOVERNED_METADATA_LIMIT]:
                    if not isinstance(item, dict) or not str(item.get('candidate_id', '')).strip():
                        continue
                    metadata['review_gated'].append({
                        'candidate_id': str(item['candidate_id'])[:64],
                        'reasons': _safe_reason_codes(item.get('reasons', [])),
                    })
            metadata['authorized'] = [
                {
                    'candidate_id': candidate_id,
                    'job_id': job_id,
                }
                for candidate_id, job_id in zip(
                    metadata['authorized_candidate_ids'],
                    metadata['queued_job_ids'],
                )
            ]
        else:
            # A governed cycle must never claim authorization without the
            # bounded audit gate producing its safe accounting record.
            metadata['gate_blockers'] = ['metadata_execution_summary_unavailable']

    review_gated = [
        {
            'action': 'publish',
            'status': 'review_gated',
            'reason': (
                'Content publishing remains review-gated; the governed '
                'full cycle does not publish content'
                if mode == 'governed'
                else 'Publishing was not attempted by the read-only full cycle'
            ),
        },
        {
            'action': 'paid_visibility',
            'status': 'not_run',
            'reason': 'Paid visibility collection requires a separate budgeted workflow',
        },
        {
            'action': 'content_body_mutations',
            'status': 'review_gated',
            'reason': 'Unreviewed content-body mutations are not authorized by the full cycle',
        },
        {
            'action': 'remote_mutations',
            'status': 'not_run',
            'reason': (
                'Only policy-authorized metadata candidates and the bounded '
                'content-autopilot publication may run; other remote mutations '
                'are not run'
                if mode in {'governed', 'autopilot'}
                else 'No remote mutation is performed by the full cycle'
            ),
        },
    ]
    gates = {item['action']: item for item in review_gated}
    summary = {
        'mode': mode,
        'metadata': metadata,
        # Keep the bounded list for generic clients and expose named gates for
        # the browser contract so the exact server reasons are not discarded.
        'review_gated': review_gated,
        'content_publishing': gates['publish'],
        'paid_visibility': gates['paid_visibility'],
        'remote_mutations': gates['remote_mutations'],
    }
    if mode == 'autopilot':
        content = _full_cycle_content_publishing(stages)
        summary['content_publishing'] = content
        summary['review_gated'][0] = content
    return summary


def _full_cycle_next_actions(stages, mode='read_only', execution_summary=None):
    actions = []
    inventory = next((stage for stage in stages if stage['name'] == 'inventory'), None)
    if inventory and inventory['status'] == 'needs_connection':
        actions.append({
            'action': 'connect_wordpress',
            'status': 'needs_connection',
            'reason': 'Verify a WordPress connection before importing the authenticated inventory',
        })
    incomplete = [stage['name'] for stage in stages if stage['status'] != 'complete']
    if incomplete:
        actions.append({
            'action': 'review_incomplete_stages',
            'status': 'needs_review',
            'stages': incomplete,
        })
    else:
        actions.append({
            'action': 'review_results',
            'status': 'ready',
            'reason': 'Review evidence and proposed work before authorizing any governed action',
        })
    # These are deliberately explicit. A full cycle never turns a policy
    # setting or a stored credential into an unreviewed remote write.
    if mode == 'read_only':
        # Preserve the historical read-only response for existing clients.
        actions.extend([
            {
                'action': 'publish',
                'status': 'policy_gated',
                'reason': 'Publishing was not attempted by the read-only full cycle',
            },
            {
                'action': 'metadata_writes',
                'status': 'policy_gated',
                'reason': 'Metadata writes were not attempted by the read-only full cycle',
            },
            {
                'action': 'paid_visibility',
                'status': 'not_run',
                'reason': 'Paid visibility collection requires a separate budgeted workflow',
            },
            {
                'action': 'remote_mutations',
                'status': 'not_run',
                'reason': 'No remote mutation is performed by the full cycle',
            },
        ])
        return actions

    metadata = (execution_summary or {}).get('metadata', {})
    queued_job_ids = _full_cycle_id_list(metadata.get('queued_job_ids'))
    if mode == 'autopilot':
        content = (execution_summary or {}).get('content_publishing')
        if not isinstance(content, dict):
            content = _full_cycle_content_publishing(stages)
        actions.append({
            key: value for key, value in content.items()
            if key in {'action', 'status', 'next_action', 'reason', 'blockers', 'article_id'}
        })
        actions.append({
            'action': 'metadata_writes',
            'status': 'queued' if queued_job_ids else 'review_gated',
            'reason': (
                'Only policy-authorized metadata candidates were queued; the '
                'candidate worker will recheck policy, readiness, and source freshness'
                if queued_job_ids
                else 'No metadata candidate was queued; review the autopilot metadata gate summary'
            ),
            'candidate_ids': _full_cycle_id_list(metadata.get('authorized_candidate_ids')),
            'job_ids': queued_job_ids,
        })
        actions.extend([
            {
                'action': 'paid_visibility',
                'status': 'not_run',
                'reason': 'Paid visibility collection requires a separate budgeted workflow',
            },
            {
                'action': 'remote_mutations',
                'status': 'not_run',
                'reason': 'Only policy-authorized metadata candidates and the content-autopilot handler may perform gated writes',
            },
        ])
        return actions

    actions.extend([
        {
            'action': 'publish',
            'status': 'review_gated',
            'reason': 'Content publishing remains review-gated and was not attempted',
        },
        {
            'action': 'metadata_writes',
            'status': 'queued' if queued_job_ids else 'review_gated',
            'reason': (
                'Only policy-authorized metadata candidates were queued; the '
                'candidate worker will recheck policy, readiness, and source freshness'
                if queued_job_ids
                else 'No metadata candidate was queued; review the governed metadata gate summary'
            ),
            'candidate_ids': _full_cycle_id_list(metadata.get('authorized_candidate_ids')),
            'job_ids': queued_job_ids,
        },
        {
            'action': 'paid_visibility',
            'status': 'not_run',
            'reason': 'Paid visibility collection requires a separate budgeted workflow',
        },
        {
            'action': 'remote_mutations',
            'status': 'not_run',
            'reason': 'Content publishing, AI generation, paid visibility, and other remote mutations were not run',
        },
    ])
    return actions


def _full_cycle_held_result(job, mode, reason):
    """Build a safe result for an autopilot parent held before any stage runs."""

    execution_summary = _full_cycle_execution_summary(mode, [])
    return {
        'workflow': 'full_cycle',
        'mode': mode,
        'status': 'held',
        'complete': False,
        'reason': reason,
        'stages': [],
        'execution_summary': execution_summary,
        'next_actions': [
            {
                'action': 'resume_full_cycle',
                'status': 'held',
                'reason': reason,
            },
            {
                'action': 'publish',
                'status': 'not_run',
                'next_action': 'resume_full_cycle',
                'reason': 'Content publishing is held until the pause is cleared',
            },
        ],
    }


async def full_cycle(db, site, job):
    """Run the bounded site understanding workflow in one job."""

    mode = full_cycle_mode(job.payload)
    pause_reason = _full_cycle_pause_reason(db, site, mode)
    if pause_reason:
        result = _full_cycle_held_result(job, mode, pause_reason)
        job.result = result
        job.updated_at = now()
        event(db, site, 'full_cycle_held', 'Full cycle held by pause control', {
            'job_id': job.id,
            'mode': mode,
            'reason': pause_reason,
        })
        db.commit()
        return result

    stage_jobs = [
        (stage_name, handler_name, _full_cycle_stage_job(db, site, job, stage_name))
        for stage_name, handler_name in _full_cycle_stage_specs(mode)
    ]
    db.commit()
    event(db, site, 'full_cycle_started', 'Full cycle started', {
        'job_id': job.id,
        'mode': mode,
        'stage_job_ids': [stage_job.id for _, _, stage_job in stage_jobs],
    })
    db.commit()

    stages = []
    complete = True
    stage_count = len(stage_jobs)
    for stage_index, (stage_name, handler_name, stage_job) in enumerate(stage_jobs, 1):
        stage_start_percent = ((stage_index - 1) * 100) // stage_count
        stage_complete_percent = (stage_index * 100) // stage_count
        # A direct retry/resume of the same parent reuses successful stage
        # results rather than creating duplicate content-plan records.
        if stage_job.status == 'complete' and isinstance(stage_job.result, dict):
            emit_job_progress(
                db,
                site,
                job_id=job.id,
                job_kind=job.kind,
                status='complete',
                phase='stage_complete',
                stage=stage_name,
                stage_index=stage_index,
                stage_count=stage_count,
                percent=stage_complete_percent,
                message=f'Full cycle stage {stage_name} complete',
            )
            db.commit()
            stage_entry = _full_cycle_stage_entry(
                stage_name, stage_job, 'complete', result=stage_job.result,
            )
            if stage_name == 'content_autopilot':
                stage_entry['content_publishing'] = _full_cycle_content_publishing([stage_entry])
            stages.append(stage_entry)
            continue

        emit_job_progress(
            db,
            site,
            job_id=job.id,
            job_kind=job.kind,
            status='running',
            phase='stage_start',
            stage=stage_name,
            stage_index=stage_index,
            stage_count=stage_count,
            percent=stage_start_percent,
            message=f'Full cycle stage {stage_name} started',
        )
        db.commit()

        if stage_name == 'inventory':
            connection = find_connection(db, site.id, 'wordpress')
            verified = bool(
                connection is not None
                and connection.status == 'connected'
                and connection.encrypted_credentials
            )
            if not verified:
                stage_job.status = 'needs_connection'
                stage_job.result = {
                    'complete': False,
                    'status': 'needs_connection',
                    'reason': 'verified_wordpress_connection_required',
                }
                stage_job.updated_at = now()
                db.commit()
                emit_job_progress(
                    db,
                    site,
                    job_id=job.id,
                    job_kind=job.kind,
                    status='needs_connection',
                    phase='stage_needs_connection',
                    stage=stage_name,
                    stage_index=stage_index,
                    stage_count=stage_count,
                    percent=stage_start_percent,
                    message='Full cycle stage inventory needs a WordPress connection',
                )
                db.commit()
                stages.append(_full_cycle_stage_entry(
                    stage_name,
                    stage_job,
                    'needs_connection',
                    result=stage_job.result,
                    reason='verified_wordpress_connection_required',
                ))
                complete = False
                continue

        stage_job.status = 'running'
        stage_job.attempts = (stage_job.attempts or 0) + 1
        stage_job.updated_at = now()
        db.commit()
        handler = HANDLERS.get(handler_name)
        try:
            if not callable(handler):
                raise RuntimeError('stage handler unavailable')
            result = await handler(db, site, stage_job)
            if not isinstance(result, dict):
                result = {'value': result}
            stage_complete = result.get('complete') is not False
            stage_job.status = 'complete' if stage_complete else 'partial'
            stage_job.result = result
            stage_job.lease_until = None
            stage_job.updated_at = now()
            db.commit()
            emit_job_progress(
                db,
                site,
                job_id=job.id,
                job_kind=job.kind,
                status=stage_job.status,
                phase='stage_complete',
                stage=stage_name,
                stage_index=stage_index,
                stage_count=stage_count,
                percent=stage_complete_percent,
                message=f'Full cycle stage {stage_name} complete',
            )
            db.commit()
            stage_entry = _full_cycle_stage_entry(
                stage_name,
                stage_job,
                stage_job.status,
                result=result,
            )
            if stage_name == 'content_autopilot':
                stage_entry['content_publishing'] = _full_cycle_content_publishing([stage_entry])
            stages.append(stage_entry)
            if not stage_complete:
                complete = False
        except Exception as exc:
            # Handlers may have committed their own read-only evidence before
            # failing.  The stage itself is still recorded as incomplete, and
            # only a stable reason is returned to avoid leaking credentials or
            # provider response details.
            db.rollback()
            stage_job = db.get(Job, stage_job.id)
            if stage_job is None or stage_job.site_id != site.id:
                raise
            reason = _full_cycle_safe_reason(stage_name, exc)
            stage_job.status = 'failed'
            stage_job.result = {'complete': False, 'status': 'failed'}
            stage_job.lease_until = None
            stage_job.updated_at = now()
            db.commit()
            emit_job_progress(
                db,
                site,
                job_id=job.id,
                job_kind=job.kind,
                status='failed',
                phase='stage_failure',
                stage=stage_name,
                stage_index=stage_index,
                stage_count=stage_count,
                percent=stage_start_percent,
                message=f'Full cycle stage {stage_name} failed',
            )
            db.commit()
            stage_entry = _full_cycle_stage_entry(
                stage_name,
                stage_job,
                'failed',
                result=stage_job.result,
                reason=reason,
            )
            if stage_name == 'content_autopilot':
                stage_entry['content_publishing'] = _full_cycle_content_publishing([stage_entry])
            stages.append(stage_entry)
            complete = False

    execution_summary = _full_cycle_execution_summary(mode, stages)
    result = {
        'workflow': 'full_cycle',
        'mode': mode,
        'complete': complete,
        'stages': stages,
        'execution_summary': execution_summary,
        'next_actions': _full_cycle_next_actions(mode=mode, stages=stages,
                                                  execution_summary=execution_summary),
    }
    event(db, site, 'full_cycle_finished', 'Full cycle finished', {
        'job_id': job.id,
        'mode': mode,
        'complete': complete,
        'stage_statuses': {stage['name']: stage['status'] for stage in stages},
        'metadata_authorized_count': execution_summary['metadata']['authorized_count'],
        'metadata_queued_count': execution_summary['metadata']['queued_count'],
    })
    db.commit()
    return result


HANDLERS = {'inventory':inventory,'poll_changes':poll_changes,'audit':audit,'availability':availability,'connection_test':connection_test,
             'plan':plan,'generate':generate,'publish':publish,'candidate':candidate,'visibility':visibility,
             'rollback':rollback,'refresh':refresh,'targeted_audit':targeted_audit,'full_cycle':full_cycle,
             'content_autopilot':content_autopilot,'reconcile_publication':reconcile_publication}
