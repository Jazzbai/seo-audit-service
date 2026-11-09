"""Read-only, site-grant-bound Google property choices for the dashboard."""
import re
import httpx

from app.intelligence.visibility import _default_transport, _oauth_access, _refresh_oauth


class GooglePropertyError(ValueError):
    pass


async def discover_properties(kind, credentials, config, *, transport=None):
    if kind not in {'gsc', 'ga4'}:
        raise GooglePropertyError('Unsupported Google property source')
    async with httpx.AsyncClient(transport=transport or _default_transport(), timeout=15,
                                 trust_env=False, follow_redirects=False) as client:
        token, refreshed, error = await _oauth_access(client, credentials, config)
        if error or not token:
            raise GooglePropertyError('Reconnect this site with Google to list its properties')
        endpoint = ('https://analyticsadmin.googleapis.com/v1beta/accountSummaries' if kind == 'ga4'
                    else 'https://searchconsole.googleapis.com/webmasters/v3/sites')
        items, seen = [], set()
        page_token = None
        for _ in range(3):
            params = {'pageSize': 200} if kind == 'ga4' else {}
            if page_token:
                params['pageToken'] = page_token
            response = await client.get(endpoint, params=params, headers={'Authorization': f'Bearer {token}'})
            if response.status_code == 401 and not refreshed:
                token, error = await _refresh_oauth(client, credentials, config)
                if error or not token:
                    raise GooglePropertyError('Reconnect this site with Google to list its properties')
                refreshed = True
                response = await client.get(endpoint, params=params, headers={'Authorization': f'Bearer {token}'})
            if not response.is_success:
                raise GooglePropertyError('Google could not list properties. Check account access and that the Analytics Admin API is enabled.')
            try:
                payload = response.json()
            except ValueError:
                raise GooglePropertyError('Google returned an unreadable property list') from None
            if not isinstance(payload, dict):
                raise GooglePropertyError('Google returned an unreadable property list')
            if kind == 'gsc':
                rows = payload.get('siteEntry', [])
            else:
                accounts = payload.get('accountSummaries', [])
                if not isinstance(accounts, list):
                    raise GooglePropertyError('Google returned an unreadable account list')
                rows = []
                for account in accounts:
                    if not isinstance(account, dict) or not isinstance(account.get('propertySummaries', []), list):
                        raise GooglePropertyError('Google returned an unreadable property list')
                    rows.extend(account.get('propertySummaries', []))
            if not isinstance(rows, list):
                raise GooglePropertyError('Google returned an unreadable property list')
            for row in rows:
                if not isinstance(row, dict):
                    continue
                value = row.get('siteUrl') if kind == 'gsc' else row.get('property')
                if not isinstance(value, str):
                    continue
                if kind == 'ga4':
                    match = re.fullmatch(r'properties/(\d+)', value)
                    if not match:
                        continue
                    value = match[1]
                if value in seen:
                    continue
                seen.add(value)
                label = row.get('displayName') if kind == 'ga4' else value
                items.append({'value': value[:2048], 'label': str(label or value)[:200]})
                if len(items) == 500:
                    return {'items': items, 'complete': False}
            page_token = payload.get('nextPageToken') if kind == 'ga4' else None
            if not page_token:
                return {'items': items, 'complete': True}
            if not isinstance(page_token, str) or len(page_token) > 2048:
                raise GooglePropertyError('Google returned an unreadable property cursor')
        return {'items': items, 'complete': False}
