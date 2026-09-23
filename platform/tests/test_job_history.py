"""Focused contract tests for authenticated site job history."""

from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app
from app.models import Job, Site, Team
from test_platform import platform  # Reuse the existing authenticated fixture.


def _job(site_id: str, job_id: str, created_at: datetime, idempotency_key: str) -> Job:
    return Job(
        id=job_id,
        site_id=site_id,
        kind="audit",
        status="complete",
        payload={"job_id": job_id},
        result={"complete": True},
        idempotency_key=idempotency_key,
        attempts=1,
        available_at=created_at,
        created_at=created_at,
        updated_at=created_at,
    )


def test_job_history_is_paginated_newest_first_and_collection_safe(platform):
    client, factory, site_id = platform
    timestamps = [
        datetime(2026, 9, 3, 12, 0, 0),
        datetime(2026, 9, 2, 12, 0, 0),
        datetime(2026, 9, 1, 12, 0, 0),
    ]
    newest, middle, oldest = (
        _job(site_id, job_id, timestamp, f"private-{job_id}-key")
        for job_id, timestamp in zip(
            ("job-newest", "job-middle", "job-oldest"), timestamps
        )
    )
    with factory() as db:
        db.add_all([oldest, newest, middle])
        db.commit()

    url = f"/api/v1/sites/{site_id}/jobs"
    response = client.get(url, params={"limit": 2, "offset": 1})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "total"}
    assert body["total"] == 3
    assert [item["id"] for item in body["items"]] == [middle.id, oldest.id]
    expected_collection_fields = {
        "id",
        "site_id",
        "kind",
        "status",
        "payload",
        "result",
        "attempts",
        "available_at",
        "lease_until",
        "created_at",
        "updated_at",
    }
    for item in body["items"]:
        assert set(item) == expected_collection_fields
        assert "idempotency_key" not in item
        assert "encrypted_credentials" not in item
        assert "private-" not in response.text

    detail = client.get(f"{url}/{newest.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["idempotency_key"] == "private-job-newest-key"

    assert client.get(url, params={"limit": 0}).status_code == 422
    assert client.get(url, params={"limit": 201}).status_code == 422
    assert client.get(url, params={"offset": -1}).status_code == 422


def test_job_history_redacts_nested_credential_shaped_values(platform):
    client, factory, site_id = platform
    row = _job(
        site_id,
        "job-secret-shaped",
        datetime(2026, 9, 4, 12, 0, 0),
        "secret-shaped-job-key",
    )
    row.payload = {
        "request": {
            "credentials": {"api_key": "payload-api-secret"},
            "note": "safe operational context",
        }
    }
    row.result = {
        "provider": {"access_token": "result-access-secret", "status": "complete"},
        "items": [{"password": "nested-password-secret", "name": "kept"}],
    }
    with factory() as db:
        db.add(row)
        db.commit()

    collection = client.get(f"/api/v1/sites/{site_id}/jobs")
    detail = client.get(f"/api/v1/sites/{site_id}/jobs/{row.id}")
    assert collection.status_code == 200, collection.text
    assert detail.status_code == 200, detail.text
    for response in (collection, detail):
        assert "payload-api-secret" not in response.text
        assert "result-access-secret" not in response.text
        assert "nested-password-secret" not in response.text
        assert "[redacted]" in response.text

    item = collection.json()["items"][0]
    assert item["payload"]["request"]["credentials"] == "[redacted]"
    assert item["result"]["provider"]["access_token"] == "[redacted]"
    assert item["result"]["items"][0]["password"] == "[redacted]"
    assert detail.json()["idempotency_key"] == "secret-shaped-job-key"


def test_job_history_is_site_scoped_and_team_authorized(platform):
    client, factory, site_id = platform
    second_site = client.post(
        "/api/v1/sites",
        json={"name": "Second site", "origin": "https://second.example.test"},
    )
    assert second_site.status_code == 201, second_site.text
    second_site_id = second_site.json()["id"]

    with factory() as db:
        site_job = _job(
            site_id,
            "job-site-a",
            datetime(2026, 9, 3, 12, 0, 0),
            "site-a-history-key",
        )
        other_site_job = _job(
            second_site_id,
            "job-site-b",
            datetime(2026, 9, 4, 12, 0, 0),
            "site-b-history-key",
        )
        other_team = Team(name="Unrelated team")
        db.add(other_team)
        db.flush()
        unrelated_site = Site(
            team_id=other_team.id,
            name="Unrelated site",
            origin="https://unrelated.example.test",
        )
        db.add(unrelated_site)
        db.flush()
        unrelated_job = _job(
            unrelated_site.id,
            "job-unrelated",
            datetime(2026, 9, 5, 12, 0, 0),
            "unrelated-history-key",
        )
        db.add_all([site_job, other_site_job, unrelated_job])
        db.commit()

    first_history = client.get(f"/api/v1/sites/{site_id}/jobs")
    assert first_history.status_code == 200, first_history.text
    assert first_history.json()["total"] == 1
    assert [item["id"] for item in first_history.json()["items"]] == [site_job.id]
    assert client.get(f"/api/v1/sites/{site_id}/jobs/{other_site_job.id}").status_code == 404

    second_history = client.get(f"/api/v1/sites/{second_site_id}/jobs")
    assert second_history.status_code == 200, second_history.text
    assert [item["id"] for item in second_history.json()["items"]] == [other_site_job.id]

    assert client.get(f"/api/v1/sites/{unrelated_site.id}/jobs").status_code == 404
    assert client.get(
        f"/api/v1/sites/{unrelated_site.id}/jobs/{unrelated_job.id}"
    ).status_code == 404


def test_job_history_is_readable_by_owner_editor_and_viewer_roles(platform):
    client, factory, site_id = platform
    with factory() as db:
        row = _job(
            site_id,
            "job-role-access",
            datetime(2026, 9, 3, 12, 0, 0),
            "role-access-history-key",
        )
        db.add(row)
        db.commit()

    members = (
        ("editor@example.test", "Editor", "editor", "editor history password"),
        ("viewer@example.test", "Viewer", "viewer", "viewer history password"),
    )
    for email, name, role, password in members:
        added = client.post(
            "/api/v1/team/members",
            json={"email": email, "name": name, "password": password, "role": role},
        )
        assert added.status_code == 200, added.text

    url = f"/api/v1/sites/{site_id}/jobs"
    owner_history = client.get(url)
    assert owner_history.status_code == 200, owner_history.text

    for email, _name, _role, password in members:
        role_client = TestClient(app)
        try:
            login = role_client.post(
                "/api/v1/auth/login",
                headers={"Origin": "http://testserver"},
                json={"email": email, "password": password},
            )
            assert login.status_code == 200, login.text
            history = role_client.get(url, headers={"Origin": "http://testserver"})
            assert history.status_code == 200, history.text
            assert history.json()["items"][0]["id"] == row.id
        finally:
            role_client.close()
