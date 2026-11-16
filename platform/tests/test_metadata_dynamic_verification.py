"""Read-only proof is required before recommendation churn can be excluded."""
import pytest

from app.intelligence.metadata_verification import establish_profile, normalize, source_drift
from app.workflows import protected_html


def document(card='First recommendation', *, body='Original article', count=2,
             widget_class='', widget_type='posts.classic', widget_id='4b03760'):
    cards = ''.join(f'<article class="post-{i}"><h3>{card} {i}</h3><a href="/{card}/{i}">Read</a></article>' for i in range(count))
    return ('<html><head><title>Original title</title><meta name="description" content="Original description">'
            '<meta name="robots" content="index,follow"><link rel="canonical" href="https://example.test/article/">'
            '<style>.entry{color:black}</style></head><body><main class="entry"><h1>Original article</h1>'
            f'<p>{body}</p></main><aside><h2>More Posts</h2>'
            f'<div class="elementor-element elementor-element-{widget_id} elementor-widget-posts{widget_class}" '
            f'data-id="{widget_id}" data-widget_type="{widget_type}" data-settings="{{}}">'
            '<div class="elementor-widget-container"><div class="elementor-posts-container">'
            f'{cards}</div></div></div></aside></body></html>')


def test_only_proven_rotating_recommendation_contents_are_excluded():
    a, b, c = (document(card) for card in ('First', 'Second', 'Third'))
    assert protected_html(a) != protected_html(b)
    profile = establish_profile(a, b, protected_html)
    assert len(profile) == 1
    assert profile[0]['widget_id'] == '4b03760'
    assert protected_html(normalize(c, profile)) == protected_html(normalize(a, profile))


def test_unchanged_recommendations_do_not_get_an_exemption():
    a = document()
    assert establish_profile(a, a, protected_html) == []
    assert protected_html(normalize(document('Changed after writing'), [])) != protected_html(a)


@pytest.mark.parametrize('change', [
    lambda s: s.replace('Original article</p>', 'Unexpected article edit</p>'),
    lambda s: s.replace('<h1>Original article', '<h1>Changed heading'),
    lambda s: s.replace('class="entry"', 'class="different-layout"'),
    lambda s: s.replace('color:black', 'color:red'),
    lambda s: s.replace('index,follow', 'noindex,nofollow'),
    lambda s: s.replace('https://example.test/article/', 'https://example.test/wrong/'),
    lambda s: s.replace('<h2>More Posts</h2>', '<h2>Changed section heading</h2>'),
])
def test_article_layout_styles_headings_and_indexing_remain_protected(change):
    a, b = document('First'), document('Second')
    with pytest.raises(ValueError, match='outside verified'):
        establish_profile(a, change(b), protected_html)
    profile = establish_profile(a, b, protected_html)
    assert protected_html(normalize(change(b), profile)) != protected_html(normalize(a, profile))


@pytest.mark.parametrize('kwargs', [
    {'count': 3}, {'widget_class': ' changed-layout'},
    {'widget_type': 'theme-post-content.default'}, {'widget_id': 'abcdef1'},
])
def test_widget_configuration_or_identity_cannot_change_after_writing(kwargs):
    a, b = document('First'), document('Second')
    profile = establish_profile(a, b, protected_html)
    with pytest.raises(ValueError):
        normalize(document('Third', **kwargs), profile)


@pytest.mark.parametrize('kwargs', [
    {'widget_type': 'theme-post-content.default'}, {'widget_id': 'not-an-id'},
])
def test_unrecognized_widgets_cannot_hide_read_only_drift(kwargs):
    with pytest.raises(ValueError, match='outside verified'):
        establish_profile(document('First', **kwargs), document('Second', **kwargs), protected_html)


def test_source_guard_also_protects_authoritative_content():
    before = {'title': 'Original', 'body': '<p>Original</p>', 'categories': [68], 'author_id': '1'}
    after = {**before, 'metadata': {'seo': {'forgeseo': {'description': 'Changed SEO'}}}}
    assert source_drift(before, after) == []
    assert source_drift(before, {**after, 'body': '<p>Unapproved edit</p>'}) == ['body']
    assert source_drift(before, {**after, 'categories': [47]}) == ['categories']
