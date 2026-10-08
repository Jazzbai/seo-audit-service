"""Operator-owned OAuth identity, with per-site grant binding and no secrets in views."""
from app.config import settings

MODE = '_forgeseo_google_mode'
CLIENT = '_forgeseo_google_client_id'


def platform_requested():
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID.strip() or settings.GOOGLE_OAUTH_CLIENT_SECRET.strip())


def platform_client():
    client_id = settings.GOOGLE_OAUTH_CLIENT_ID.strip()
    secret = settings.GOOGLE_OAUTH_CLIENT_SECRET.strip()
    if not client_id or not secret:
        raise ValueError('The platform administrator must finish Google connection setup')
    return client_id, secret


def runtime_credentials(saved):
    if not platform_requested() and saved.get(MODE) != 'platform':
        # Existing self-hosted installations continue to use their encrypted
        # site-specific clients until the operator explicitly enables service mode.
        return saved
    client_id, secret = platform_client()
    if saved.get(MODE) != 'platform' or saved.get(CLIENT) != client_id:
        raise ValueError('Reconnect this site with Google after the platform client changed')
    if not saved.get('refresh_token') and not saved.get('access_token'):
        raise ValueError('Authorize this site with Google before testing its connection')
    return {**{key: saved[key] for key in ('access_token', 'refresh_token', 'token_type', 'expires_at') if key in saved},
            'client_id': client_id, 'client_secret': secret,
            'token_url': 'https://oauth2.googleapis.com/token'}
