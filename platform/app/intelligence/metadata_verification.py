"""Bounded public-HTML verification for metadata-only mutations.

Elementor recommendation cards may rotate on every read. Establish that churn
before writing, and exempt only those card contents, never the article or the
widget/container configuration. Unrecognized dynamic markup still fails closed.
"""
import hashlib
import json
import re

from bs4 import BeautifulSoup


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _regions(soup):
    regions = {}
    for widget in soup.select('div.elementor-widget-posts[data-id][data-widget_type="posts.classic"]'):
        identity = widget.get('data-id', '')
        if not re.fullmatch(r'[a-f0-9]{6,8}', identity):
            continue
        if 'elementor-element-' + identity not in widget.get('class', []):
            continue
        containers = widget.select(':scope > .elementor-widget-container > .elementor-posts-container')
        if len(containers) != 1 or identity in regions:
            raise ValueError('Recommendation region is not unique')
        container = containers[0]
        cards = container.find_all('article', recursive=False)
        if not 1 <= len(cards) <= 12 or len(container.find_all(recursive=False)) != len(cards):
            continue
        regions[identity] = (widget, container, cards)
    return regions


def establish_profile(first_html, second_html, fingerprint):
    first = BeautifulSoup(first_html, 'html.parser')
    second = BeautifulSoup(second_html, 'html.parser')
    left, right = _regions(first), _regions(second)
    profile = []
    for identity, (widget, container, cards) in left.items():
        if identity not in right:
            continue
        other_widget, other_container, other_cards = right[identity]
        if str(container) == str(other_container):
            continue
        if (widget.attrs != other_widget.attrs or container.attrs != other_container.attrs
                or len(cards) != len(other_cards)):
            raise ValueError('Recommendation configuration changed during baseline reads')
        profile.append({'widget_id': identity, 'widget_attributes': _hash(widget.attrs),
                        'container_attributes': _hash(container.attrs), 'card_count': len(cards)})
    if len(profile) > 4:
        raise ValueError('Too many changing recommendation regions')
    if fingerprint(normalize(first_html, profile)) != fingerprint(normalize(second_html, profile)):
        raise ValueError('Public page changed outside verified recommendation cards')
    return profile


def normalize(document, profile):
    if not profile:
        return document
    soup = BeautifulSoup(document, 'html.parser')
    available = _regions(soup)
    for rule in profile:
        if rule['widget_id'] not in available:
            raise ValueError('Verified recommendation region disappeared')
        widget, container, cards = available[rule['widget_id']]
        if (_hash(widget.attrs) != rule['widget_attributes']
                or _hash(container.attrs) != rule['container_attributes']
                or len(cards) != rule['card_count']):
            raise ValueError('Verified recommendation configuration changed')
        container.clear()
        container.append(soup.new_tag('span', attrs={'data-jazzb-verification': 'rotating-recommendations'}))
    return str(soup)


def source_drift(before, after):
    """Metadata writes must preserve authoritative native content too."""
    fields = ('title', 'body', 'excerpt', 'slug', 'status', 'author_id',
              'categories', 'tags', 'featured_media')
    return [key for key in fields if before.get(key) != after.get(key)]
