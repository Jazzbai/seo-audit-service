"""Source crawl completion must never imply successful rendered inspection."""
from datetime import timedelta

import pytest

from app.models import Job
from app.operations import now
from test_platform import platform


def add_browser(db, site_id, page_id, *, status='complete', result=None, age_days=0):
    row = Job(site_id=site_id, kind='browser', status=status, payload={'page_id': page_id},
              result=result or {}, idempotency_key=f'{site_id}-{page_id}-{age_days}-{status}',
              created_at=now()-timedelta(days=age_days), updated_at=now()-timedelta(days=age_days))
    db.add(row)
    return row


def test_source_complete_is_separate_from_partial_rendering(platform):
    client, factory, site_id = platform
    with factory() as db:
        db.add(Job(site_id=site_id, kind='audit', status='complete',
                   result={'complete': True, 'pending_urls': [], 'errors': []}, idempotency_key='source-complete'))
        add_browser(db, site_id, 'one', status='partial', result={'complete': False, 'status_code': 200, 'resource_failures': 13})
        db.commit()
    view = client.get(f'/api/v1/sites/{site_id}/overview').json()
    assert view['coverage']['status'] == 'complete'
    assert view['browser_coverage']['status'] == 'partial'
    assert view['browser_coverage']['complete_samples'] == 0
    assert view['browser_coverage']['partial_samples'] == 1


@pytest.mark.parametrize('result', [{}, {'complete': True, 'status_code': 200, 'resource_failures': 2},
                                  {'complete': True, 'status_code': True, 'resource_failures': False},
                                  {'complete': True, 'status_code': 404, 'resource_failures': 0}])
def test_complete_job_without_complete_render_evidence_is_unverified(platform, result):
    client, factory, site_id = platform
    with factory() as db:
        add_browser(db, site_id, 'one', result=result)
        db.commit()
    view = client.get(f'/api/v1/sites/{site_id}/overview').json()['browser_coverage']
    assert view['status'] == 'needs_review'
    assert view['complete_samples'] == 0


def test_render_summary_uses_latest_page_evidence_and_not_stale_jobs(platform):
    client, factory, site_id = platform
    with factory() as db:
        add_browser(db, site_id, 'one', status='partial', age_days=1)
        add_browser(db, site_id, 'one', result={'complete': True, 'status_code': 200, 'resource_failures': 0})
        add_browser(db, site_id, 'old', status='partial', age_days=8)
        db.commit()
    view = client.get(f'/api/v1/sites/{site_id}/overview').json()['browser_coverage']
    assert view['status'] == 'samples_complete'
    assert view['sample_count'] == view['complete_samples'] == 1
    assert 'every page' in view['message']


def test_queued_render_is_pending_not_covered(platform):
    client, factory, site_id = platform
    with factory() as db:
        add_browser(db, site_id, 'one', status='queued')
        db.commit()
    view = client.get(f'/api/v1/sites/{site_id}/overview').json()['browser_coverage']
    assert view['status'] == 'pending'
    assert view['complete_samples'] == 0
    assert view['pending_samples'] == 1
