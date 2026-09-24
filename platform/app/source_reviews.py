"""Revision-bound editorial attestations; provider provenance stays immutable."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit


def source_url(value):
    value = value.get('url') if isinstance(value, dict) else value
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username or parts.password or parts.port not in (None, 80, 443)):
            return None
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or '/', parts.query, ''))
    except ValueError:
        return None


def review_fingerprint(article):
    brief = article.get('brief') or {}
    generation = article.get('provenance') or brief.get('generation') or {}
    # Attribution-only edits do not change source support. Body, source-list or
    # generation changes do; reviews are never transferable to another article.
    value = {key: article.get(key) for key in ('id', 'site_id', 'title', 'body', 'sources')}
    value['generation'] = generation
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def reviewed_source_urls(article):
    fingerprint = review_fingerprint(article)
    current = datetime.now(timezone.utc)
    reviews = (article.get('brief') or {}).get('source_reviews', [])
    if not isinstance(reviews, list):
        return set()
    accepted = set()
    for review in reviews:
        if not isinstance(review, dict):
            continue
        try:
            reviewed_at = datetime.fromisoformat(review['reviewed_at'].replace('Z', '+00:00'))
            fresh = current - timedelta(days=7) <= reviewed_at <= current + timedelta(seconds=60)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        url = source_url(review.get('url'))
        if (fresh and url and review.get('kind') == 'authenticated_source_review'
                and review.get('decision') == 'accepted_for_this_revision'
                and review.get('article_fingerprint') == fingerprint
                and review.get('reviewer_id') and review.get('notes')
                and review.get('http_status') == 200
                and re.fullmatch(r'[a-f0-9]{64}', str(review.get('content_sha256', '')))):
            accepted.add(url)
    return accepted
