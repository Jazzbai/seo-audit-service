"""Expose measured counts, never token credentials or untrusted usage strings."""

import json

import pytest

from app.api import _safe_job_value, article_view, measurement_view, job_history_view
from app.models import Article, Job, Measurement


USAGE = {
    'input_tokens': 4279,
    'output_tokens': 856,
    'total_tokens': 5135,
    'input_tokens_details': {'cached_tokens': 0},
    'output_tokens_details': {'reasoning_tokens': 0},
}


@pytest.mark.parametrize('wrapper', [
    lambda usage: {'generation': {'usage': usage}},
    lambda usage: {'_collection': {'usage': [usage]}},
])
def test_only_numeric_usage_is_public(wrapper):
    value = wrapper(USAGE)
    assert _safe_job_value(value) == value


@pytest.mark.parametrize('value', [
    'secret-token-shaped-as-count', True, -1, 1.5, None,
    {'api_key': 'secret-token-shaped-as-count'},
    ['secret-token-shaped-as-count'], 2**53,
])
def test_malformed_token_counts_stay_redacted(value):
    assert _safe_job_value({'usage': {'input_tokens': value}}) == {
        'usage': {'input_tokens': '[redacted]'},
    }


def test_usage_does_not_open_a_credential_redaction_escape():
    value = {
        'usage': {
            **USAGE,
            'api_key': 'do-not-expose',
            'access_token': 123456,
            'input_tokens_details': {'cached_tokens': 0, 'refreshToken': 'do-not-expose'},
            'input_tokens_secret': 123456,
        },
        'input_tokens': 123456,
        'authorization': {'usage': USAGE},
        'session_token_usage': USAGE,
    }
    safe = _safe_job_value(value)
    assert safe['usage']['input_tokens'] == 4279
    assert safe['usage']['input_tokens_details'] == {'cached_tokens': 0, 'refreshToken': '[redacted]'}
    assert safe['usage']['api_key'] == '[redacted]'
    assert safe['usage']['access_token'] == '[redacted]'
    assert safe['usage']['input_tokens_secret'] == '[redacted]'
    assert safe['input_tokens'] == '[redacted]'
    assert safe['authorization'] == '[redacted]'
    assert safe['session_token_usage'] == '[redacted]'
    assert 'do-not-expose' not in json.dumps(safe)


def test_usage_keeps_depth_limit():
    value = {'usage': USAGE}
    for _ in range(20):
        value = {'usage': value}
    safe = _safe_job_value(value)
    assert '4279' not in json.dumps(safe)
    assert '[redacted]' in json.dumps(safe)


def test_article_job_and_visibility_use_the_same_safe_counts():
    article = Article(id='article', site_id='site', title='Review only', body='',
                      brief={'generation': {'usage': USAGE}}, sources=[])
    job = Job(id='job', site_id='site', kind='generate', payload={},
              result={'usage': USAGE}, idempotency_key='private-operation')
    sample = Measurement(id='sample', site_id='site', kind='ai_sample',
                         data={'_collection': {'usage': [USAGE]}})
    assert article_view(article)['brief']['generation']['usage'] == USAGE
    assert job_history_view(job)['result']['usage'] == USAGE
    assert 'idempotency_key' not in job_history_view(job)
    assert measurement_view(sample)['data']['_collection']['usage'] == [USAGE]
