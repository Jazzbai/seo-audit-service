"""Fresh WordPress author observations, never authorization from old inventory."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from hashlib import sha256

from app.operations import find_connection, iso, now, _parse_timestamp


AUTHOR_FRESHNESS_SECONDS = 300
DISCOVERY_CODES = frozenset({'author_listing_incomplete', 'author_listing_denied',
    'connection_capabilities_incomplete', 'author_records_incomplete', 'connection_cannot_publish'})


class AuthorVerificationError(ValueError):
    pass


def connection_fingerprint(connection) -> str:
    return sha256((connection.encrypted_credentials or '').encode()).hexdigest()


def unavailable(reason: str) -> dict:
    return {'items': [], 'complete': False, 'checked_at': iso(now()),
            'authenticated_user_id': None, 'blockers': [reason]}


def current_authors(connection) -> dict:
    """A short-lived observation is useful for preflight, never the final write."""
    if connection is None or connection.status == 'revoked' or not connection.encrypted_credentials:
        return unavailable('wordpress_connection_required')
    saved = (connection.capabilities or {}).get('author_discovery')
    if not isinstance(saved, dict):
        return unavailable('author_discovery_required')
    checked = _parse_timestamp(saved.get('checked_at'))
    if (checked is None or checked > now() + timedelta(seconds=30)
            or now() - checked > timedelta(seconds=AUTHOR_FRESHNESS_SECONDS)
            or saved.get('connection_fingerprint') != connection_fingerprint(connection)):
        return unavailable('author_discovery_stale')
    return {key: value for key, value in saved.items() if key != 'connection_fingerprint'}


def author_ids(observation: dict) -> set[str]:
    if observation.get('complete') is not True or observation.get('blockers'):
        return set()
    return {str(item['id']) for item in observation.get('items', [])
            if isinstance(item, dict) and str(item.get('id', '')).isdigit()}


async def refresh_authors(db, site) -> dict:
    from app.workflows import client_for

    connection = find_connection(db, site.id, 'wordpress')
    if connection is None or connection.status == 'revoked' or not connection.encrypted_credentials:
        return unavailable('wordpress_connection_required')
    initial_fingerprint = connection_fingerprint(connection)
    try:
        async with await client_for(db, site) as client:
            result = await asyncio.wait_for(client.discover_authors(), timeout=25)
        if not isinstance(result, dict) or not isinstance(result.get('items'), list):
            result = unavailable('author_discovery_incomplete')
        elif result.get('complete') is not True or result.get('blockers'):
            codes = [code for code in result.get('blockers', []) if code in DISCOVERY_CODES]
            result = {**unavailable('author_discovery_incomplete'),
                      'blockers': codes or ['author_discovery_incomplete']}
        else:
            items = result['items']
            if any(not isinstance(item, dict) or not str(item.get('id', '')).isdigit()
                   or not isinstance(item.get('name'), str) for item in items):
                result = unavailable('author_discovery_incomplete')
            else:
                result = {'items': [{'id': str(item['id']), 'name': item['name'][:200]} for item in items],
                          'complete': True, 'checked_at': iso(now()),
                          'authenticated_user_id': result.get('authenticated_user_id'), 'blockers': [],
                          'warnings': ['connection_can_only_assign_self'] if 'connection_can_only_assign_self' in result.get('warnings', []) else []}
    except Exception as exc:
        # Provider responses may contain private users or connection material.
        reason = 'wordpress_authentication_failed' if getattr(exc, 'status_code', None) == 401 else 'author_discovery_failed'
        result = unavailable(reason)
    db.refresh(connection)
    if connection.status == 'revoked' or connection_fingerprint(connection) != initial_fingerprint:
        return unavailable('wordpress_connection_changed')
    connection.capabilities = {**(connection.capabilities or {}), 'author_discovery': {
        **result, 'connection_fingerprint': initial_fingerprint,
    }}
    db.flush()
    return result


def apply_author_check(checks: dict, author_id, observation: dict) -> dict:
    """Replace inventory-only author assertions, without waiving other checks."""
    blockers = [code for code in checks.get('blockers', []) if code != 'author_not_verified']
    if author_id and str(author_id) not in author_ids(observation):
        blockers.append('author_not_verified')
    return {**checks, 'passed': not blockers, 'blockers': list(dict.fromkeys(blockers)),
            'author_discovery': observation}


async def require_current_author(client, author_id) -> None:
    """Called immediately before remote draft creation and publication."""
    try:
        result = await asyncio.wait_for(client.discover_authors(), timeout=25)
    except Exception:
        raise AuthorVerificationError('WordPress author verification is unavailable') from None
    if not isinstance(result, dict) or str(author_id) not in author_ids(result):
        raise AuthorVerificationError('Selected WordPress author is no longer verified or assignable')
