import asyncio

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.intelligence.audit as audit_module
from app import workflows
from app.config import settings
from app.models import Base, Finding, Job, Site, Team
from app.intelligence.audit import reconcile_site_audit


def test_reconciliation_keeps_redirect_targets_separate_from_page_namespaces():
    result = reconcile_site_audit(
        [
            {
                "url": "https://example.test/services/brakes",
                "requested_url": "https://example.test/old-brakes",
                "redirect_chain": [
                    "https://example.test/old-brakes",
                    "https://example.test/legacy-brakes",
                    "https://example.test/services/brakes",
                ],
                "status_code": 200,
                "error": None,
                "resource_type": "pages",
                "signals": {
                    "source": "source_html",
                    "metadata": {"canonical": "https://example.test/services/brakes"},
                    "page_purpose": "informational",
                },
            },
            {
                "url": "https://example.test/services/brakes",
                "requested_url": "https://example.test/services/brakes",
                "status_code": 200,
                "error": None,
                "resource_type": "pages",
                "signals": {
                    "source": "browser",
                    "metadata": {"canonical": "https://example.test/services/brakes"},
                    "page_purpose": "informational",
                },
            },
            {
                "url": "https://example.test/repair-a",
                "status_code": 200,
                "error": None,
                "resource_type": "pages",
                "signals": {
                    "source": "source_html",
                    "metadata": {"canonical": "https://example.test/repair-a"},
                    "page_purpose": "informational",
                },
            },
            {
                "url": "https://example.test/repair-b",
                "status_code": 200,
                "error": None,
                "resource_type": "pages",
                "signals": {
                    "source": "source_html",
                    "metadata": {"canonical": "https://example.test/repair-b"},
                    "page_purpose": "informational",
                },
            },
        ],
        "https://example.test",
        browser_sample_urls=["https://example.test/services/brakes"],
    )

    assert result["namespace"] == "site_reconciliation"
    assert result["redirects"] == [
        {
            "source_url": "https://example.test/old-brakes",
            "target_url": "https://example.test/services/brakes",
            "chain": [
                "https://example.test/old-brakes",
                "https://example.test/legacy-brakes",
                "https://example.test/services/brakes",
            ],
            "hop_count": 2,
            "status_code": 200,
            "error": None,
        }
    ]
    assert result["duplicate_urls"][0]["kind"] == "redirect_convergence"
    assert result["duplicate_urls"][0]["target_url"] == "https://example.test/services/brakes"
    assert result["canonical_collisions"] == []
    codes = {item["code"] for item in result["findings"]}
    assert {"duplicate_url_target", "redirect_chain"} <= codes
    assert all(item["namespace"] == "site_reconciliation" for item in result["findings"])
    coverage = result["template_coverage"]
    assert coverage["template_count"] == 2
    assert coverage["browser_covered_count"] == 1
    assert coverage["browser_pending_count"] == 1
    assert all(
        template["coverage_status"] in {"source_only", "source_and_browser"}
        for template in coverage["templates"]
    )


def test_reconciliation_reports_canonical_collisions_without_inventing_duplicate_content():
    result = reconcile_site_audit(
        [
            {
                "url": "https://example.test/repair-a",
                "status_code": 200,
                "error": None,
                "signals": {
                    "source": "source_html",
                    "metadata": {"canonical": "https://example.test/repair"},
                },
            },
            {
                "url": "https://example.test/repair-b",
                "status_code": 200,
                "error": None,
                "signals": {
                    "source": "source_html",
                    "metadata": {"canonical": "https://example.test/repair"},
                },
            },
        ],
        "https://example.test",
    )

    assert result["canonical_collisions"] == [
        {
            "canonical_url": "https://example.test/repair",
            "page_urls": [
                "https://example.test/repair-a",
                "https://example.test/repair-b",
            ],
            "page_count": 2,
        }
    ]
    assert {item["code"] for item in result["findings"]} == {"duplicate_canonical_target"}
    assert result["duplicate_urls"] == []


@pytest.mark.asyncio
async def test_crawl_retains_only_concrete_redirect_hop_evidence():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/robots.txt", "/sitemap.xml"}:
            return httpx.Response(404)
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/middle"})
        if request.url.path == "/middle":
            return httpx.Response(302, headers={"location": "/new"})
        if request.url.path == "/new":
            return httpx.Response(
                200,
                text="<html><head><title>New</title></head><body><main><h1>New</h1></main></body></html>",
                headers={"content-type": "text/html"},
            )
        raise AssertionError(request.url)

    result = await audit_module.crawl(
        "https://example.test",
        max_pages=1,
        transport=httpx.MockTransport(handler),
        seed_urls=["https://example.test/old"],
    )

    assert result["complete"] is True
    assert result["pages"] == [
        {
            "url": "https://example.test/new",
            "html": "<html><head><title>New</title></head><body><main><h1>New</h1></main></body></html>",
            "status_code": 200,
            "error": None,
            "requested_url": "https://example.test/old",
            "redirect_chain": [
                "https://example.test/old",
                "https://example.test/middle",
                "https://example.test/new",
            ],
        }
    ]


def test_audit_workflow_persists_site_reconciliation_findings(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(settings, "ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    with Session(engine) as db:
        team = Team(name="Audit team")
        db.add(team)
        db.flush()
        site = Site(team_id=team.id, name="Audit site", origin="https://example.test")
        db.add(site)
        db.flush()
        origin = site.origin
        job = Job(
            id="audit-reconciliation",
            site_id=site.id,
            kind="audit",
            status="running",
            payload={},
            idempotency_key="audit-reconciliation",
        )
        db.add(job)
        db.commit()

        async def crawl_once(origin, max_pages=100, transport=None, seed_urls=None, visited_urls=None):
            html = "<html><head><title>Brakes</title></head><body><main><h1>Brakes</h1></main></body></html>"
            return {
                "pages": [
                    {
                        "url": origin + "/brakes",
                        "requested_url": origin + "/old-brakes",
                        "redirect_chain": [origin + "/old-brakes", origin + "/brakes"],
                        "html": html,
                        "status_code": 200,
                        "error": None,
                    },
                    {
                        "url": origin + "/brakes",
                        "html": html,
                        "status_code": 200,
                        "error": None,
                    },
                ],
                "complete": True,
                "pending_urls": [],
                "visited_urls": [origin + "/brakes"],
                "errors": [],
            }

        monkeypatch.setattr("app.intelligence.audit.crawl", crawl_once)
        result = asyncio.run(workflows.audit(db, site, job))
        db.commit()

        assert result["reconciliation"]["redirects"][0]["target_url"] == origin + "/brakes"
        assert result["reconciliation"]["duplicate_urls"][0]["kind"] == "redirect_convergence"
        assert result["reconciliation"]["resolution_allowed"] is True
        rows = db.scalars(
            select(Finding).where(
                Finding.site_id == site.id,
                Finding.page_id.is_(None),
                Finding.status == "open",
            )
        ).all()
        assert {row.code for row in rows} == {"duplicate_url_target"}
        assert rows[0].details["namespace"] == "site_reconciliation"

    engine.dispose()
