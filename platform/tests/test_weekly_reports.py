"""Focused contract tests for the browser-facing weekly report."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from app import api
from app.models import (
    BudgetAccount,
    CostReservation,
    Incident,
    Measurement,
    Publication,
    Site,
)
from app.operations import event
from test_platform import platform


def _seed_report_rows(factory, site_id: str, *, current: datetime, other_site_id: str | None = None):
    inside = current - timedelta(days=1)
    outside = current - timedelta(days=8)
    future = current + timedelta(minutes=1)
    with factory() as db:
        site = db.get(Site, site_id)
        account = BudgetAccount(
            site_id=site_id,
            period=current.strftime('%Y-%m'),
            limit_cents=30000,
            spent_cents=725,
            reserved_cents=125,
        )
        db.add(account)
        db.flush()
        db.add_all([
            CostReservation(
                site_id=site_id,
                account_id=account.id,
                operation_key='weekly-visible-operation',
                estimated_cents=125,
                status='reserved',
                created_at=inside,
            ),
            CostReservation(
                site_id=site_id,
                account_id=account.id,
                operation_key='weekly-old-operation',
                estimated_cents=300,
                actual_cents=300,
                status='settled',
                created_at=outside,
            ),
            CostReservation(
                site_id=site_id,
                account_id=account.id,
                operation_key='weekly-future-operation',
                estimated_cents=450,
                status='reserved',
                created_at=future,
            ),
        ])
        db.add_all([
            Incident(
                site_id=site_id,
                key='monitoring:weekly-visible',
                kind='monitoring',
                severity='high',
                title='A period incident',
                status='open',
                details={
                    'safe_context': 'visible evidence',
                    'access_token': 'do-not-send-period-secret',
                },
                first_seen_at=outside,
                last_seen_at=inside,
            ),
            Incident(
                site_id=site_id,
                key='monitoring:weekly-old',
                kind='monitoring',
                severity='low',
                title='An old incident',
                status='resolved',
                details={},
                first_seen_at=outside,
                last_seen_at=outside,
                resolved_at=outside,
            ),
            Incident(
                site_id=site_id,
                key='monitoring:weekly-future',
                kind='monitoring',
                severity='low',
                title='A future incident',
                status='open',
                details={},
                first_seen_at=future,
                last_seen_at=future,
            ),
        ])
        period_event = event(db, site, 'weekly_event', 'Period activity', {'status': 'complete'})
        period_event.created_at = inside
        db.add(Measurement(
            site_id=site_id,
            kind='search_visibility',
            source='weekly-fixture',
            data={'value': 1},
            observed_at=inside,
        ))
        db.add(Publication(
            site_id=site_id,
            operation_key='weekly-publication',
            status='verified',
            policy_version=1,
            snapshot={},
            result={},
            created_at=inside,
            updated_at=inside,
        ))
        if other_site_id is not None:
            other_site = db.get(Site, other_site_id)
            other_account = BudgetAccount(
                site_id=other_site_id,
                period=current.strftime('%Y-%m'),
                limit_cents=30000,
                spent_cents=999,
                reserved_cents=999,
            )
            db.add(other_account)
            db.flush()
            db.add(CostReservation(
                site_id=other_site_id,
                account_id=other_account.id,
                operation_key='other-site-operation',
                estimated_cents=999,
                status='reserved',
                created_at=inside,
            ))
            db.add(Incident(
                site_id=other_site_id,
                key='monitoring:other-site',
                kind='monitoring',
                severity='critical',
                title='Other site incident',
                status='open',
                details={'secret': 'other-site-secret'},
                first_seen_at=inside,
                last_seen_at=inside,
            ))
        db.commit()


def test_weekly_report_returns_period_bounded_browser_safe_history_and_spending(platform, monkeypatch):
    client, factory, site_id = platform
    current = datetime(2030, 9, 21, 12, 0, 0)
    other_site = client.post(
        '/api/v1/sites',
        json={'name': 'Other report site', 'origin': 'https://other-report.example.test'},
    )
    assert other_site.status_code == 201, other_site.text
    _seed_report_rows(factory, site_id, current=current, other_site_id=other_site.json()['id'])
    monkeypatch.setattr(api, 'now', lambda: current)

    response = client.get(f'/api/v1/sites/{site_id}/reports/weekly')

    assert response.status_code == 200, response.text
    report = response.json()
    assert report['generated_at'] == '2030-09-21T12:00:00Z'
    assert report['period_start'] == '2030-09-14T12:00:00Z'
    assert report['period_end'] == report['generated_at']
    assert report['incidents']['total'] == 1
    assert report['incidents']['items'][0]['title'] == 'A period incident'
    assert report['incidents']['items'][0]['details']['safe_context'] == 'visible evidence'
    assert report['incidents']['items'][0]['details']['access_token'] == '[redacted]'
    assert report['spending']['budget_account']['spent_cents'] == 725
    assert report['spending']['reservations']['total'] == 1
    assert report['spending']['reservations']['items'][0]['operation_key'] == 'weekly-visible-operation'
    assert report['measurements']['total'] == 1
    assert report['events']['total'] == 1
    assert report['publications']['total'] == 1

    encoded = json.dumps(report)
    assert 'do-not-send-period-secret' not in encoded
    assert 'other-site-secret' not in encoded
    assert 'other-site-operation' not in encoded
    assert 'weekly-old-operation' not in encoded
    assert 'weekly-future-operation' not in encoded
    assert 'An old incident' not in encoded
    assert 'A future incident' not in encoded


def test_weekly_report_keeps_empty_and_partial_periods_explicit(platform, monkeypatch):
    client, factory, site_id = platform
    current = datetime(2030, 1, 8, 10, 0, 0)
    monkeypatch.setattr(api, 'now', lambda: current)

    empty = client.get(f'/api/v1/sites/{site_id}/reports/weekly')

    assert empty.status_code == 200, empty.text
    empty_report = empty.json()
    assert empty_report['incidents'] == {'items': [], 'total': 0}
    assert empty_report['spending']['budget_account'] is None
    assert empty_report['spending']['reservations'] == {'items': [], 'total': 0}

    with factory() as db:
        db.add(Incident(
            site_id=site_id,
            key='monitoring:partial-period',
            kind='monitoring',
            severity='medium',
            title='Only incident evidence',
            status='open',
            details={},
            first_seen_at=current - timedelta(hours=1),
            last_seen_at=current - timedelta(minutes=1),
        ))
        db.commit()

    partial = client.get(f'/api/v1/sites/{site_id}/reports/weekly')

    assert partial.status_code == 200, partial.text
    partial_report = partial.json()
    assert partial_report['incidents']['total'] == 1
    assert partial_report['spending']['budget_account'] is None
    assert partial_report['spending']['reservations'] == {'items': [], 'total': 0}


def test_weekly_report_csv_keeps_existing_event_contract(platform, monkeypatch):
    client, factory, site_id = platform
    current = datetime(2030, 9, 21, 12, 0, 0)
    monkeypatch.setattr(api, 'now', lambda: current)
    with factory() as db:
        site = db.get(Site, site_id)
        csv_event = event(db, site, 'weekly_csv_event', 'CSV-safe event', {})
        csv_event.created_at = current - timedelta(hours=1)
        db.commit()

    response = client.get(f'/api/v1/sites/{site_id}/reports/weekly?format=csv')

    assert response.status_code == 200, response.text
    assert response.headers['content-type'].startswith('text/csv')
    assert response.text.splitlines()[0] == 'time,type,message'
    assert 'CSV-safe event' in response.text
