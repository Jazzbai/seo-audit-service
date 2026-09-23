"""Run a secret-safe, read-only handoff probe against a ForgeSEO deployment.

The connector probe and the platform UI are useful at different boundaries.
This command exercises the boundary between them without turning a deployment
check into a publishing command: it logs in to the standalone platform, reads
the selected site's state, and can explicitly queue only the inventory and
public-audit jobs. Those jobs may update ForgeSEO's local evidence, but they do
not write to WordPress.

Credentials are accepted only from environment variables. The report never
contains the password, cookies, CSRF token, connection credentials, or raw job
payloads. There is deliberately no publish, candidate-execute, article, or
policy-update option in this module.

Example::

    $env:FORGE_PLATFORM_URL = "https://seo.example.com"
    $env:FORGE_PLATFORM_EMAIL = "owner@example.com"
    $env:FORGE_PLATFORM_PASSWORD = "use-a-private-shell-secret"
    python -m scripts.platform_live_probe --site-id SITE_ID --run-inventory --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


TERMINAL_JOB_STATUSES = frozenset(
    {
        "complete",
        "partial",
        "failed",
        "blocked",
        "needs_reconciliation",
        "ambiguous",
        "rolled_back",
    }
)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
READ_ONLY_JOB_KINDS = frozenset({"inventory", "audit"})


class ProbeError(RuntimeError):
    """A safe operator-facing failure without remote response contents."""


def normalize_origin(value: str) -> str:
    """Validate an origin before it becomes an HTTP target or report value."""

    if not isinstance(value, str) or not value.strip():
        raise ProbeError("the ForgeSEO deployment origin is required")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ProbeError("the ForgeSEO deployment URL must be an origin without credentials or a path")
    host = parsed.hostname.lower()
    if parsed.scheme != "https" and host not in LOCAL_HOSTS:
        raise ProbeError("HTTPS is required except for an explicitly local deployment")
    # urlsplit().netloc preserves the port while hostname removes it.
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _check(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    value.update(extra)
    return value


def _safe_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def summarize_job(job: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    """Keep only operational scalars from a browser-safe job response."""

    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    summary: dict[str, Any] = {
        "kind": kind,
        "job_id": job.get("id") if isinstance(job.get("id"), str) else None,
        "status": job.get("status") if isinstance(job.get("status"), str) else "unknown",
    }
    for field in ("complete", "resources", "seen", "missing", "error_count", "pending_url_count"):
        value = result.get(field)
        if isinstance(value, bool):
            summary[field] = value
        else:
            integer = _safe_int(value)
            if integer is not None:
                summary[field] = integer
    if isinstance(result.get("counts"), dict):
        counts: dict[str, int] = {}
        for key, value in result["counts"].items():
            if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool):
                counts[key] = value
        if counts:
            summary["counts"] = counts
    if isinstance(result.get("error_type"), str):
        summary["error_type"] = result["error_type"][:80]
    if isinstance(result.get("reason"), str):
        # Worker reasons are already browser-safe, but keep the probe bounded.
        summary["reason"] = result["reason"][:200]
    return summary


class PlatformClient:
    """Small authenticated API client with explicit read-only job surface."""

    def __init__(self, client: httpx.Client, origin: str):
        self.client = client
        self.origin = origin
        self.csrf_token: str | None = None

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Origin", self.origin)
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and self.csrf_token:
            headers.setdefault("X-CSRF-Token", self.csrf_token)
        response = self.client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            raise ProbeError(f"platform API returned HTTP {response.status_code} for {method.upper()} {path}")
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ProbeError(f"platform API returned invalid JSON for {method.upper()} {path}") from exc

    def login(self, email: str, password: str) -> dict[str, Any]:
        payload = self.request("POST", "/auth/login", json={"email": email, "password": password})
        if not isinstance(payload, dict) or not isinstance(payload.get("csrf_token"), str):
            raise ProbeError("platform login did not return a CSRF token")
        self.csrf_token = payload["csrf_token"]
        return payload

    def read_site(self, site_id: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        sites_payload = self.request("GET", "/sites")
        sites = sites_payload.get("items", []) if isinstance(sites_payload, dict) else []
        sites = [item for item in sites if isinstance(item, dict)]
        if site_id:
            selected = next((item for item in sites if item.get("id") == site_id), None)
            if selected is None:
                raise ProbeError("the requested site is not available to this account")
        elif len(sites) == 1:
            selected = sites[0]
        elif not sites:
            raise ProbeError("the account has no sites to inspect")
        else:
            raise ProbeError("--site-id is required when the account has multiple sites")
        if not isinstance(selected.get("id"), str):
            raise ProbeError("the selected site response is missing its id")
        return selected, sites

    def queue_read_only_job(self, site_id: str, kind: str) -> dict[str, Any]:
        if kind not in READ_ONLY_JOB_KINDS:
            raise ProbeError(f"unsupported probe job: {kind}")
        return self.request(
            "POST",
            f"/sites/{site_id}/jobs",
            json={
                "kind": kind,
                "payload": {},
                "idempotency_key": f"platform-live-probe:{kind}:{uuid4().hex}",
            },
        )

    def get_job(self, site_id: str, job_id: str) -> dict[str, Any]:
        value = self.request("GET", f"/sites/{site_id}/jobs/{job_id}")
        if not isinstance(value, dict):
            raise ProbeError("platform job response was not an object")
        return value


def _site_summary(site: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: site.get(key)
        for key in ("id", "name", "origin", "paused", "timezone", "language")
        if site.get(key) is not None
    }


def run_probe(
    client: PlatformClient,
    *,
    site_id: str | None = None,
    run_inventory: bool = False,
    run_audit: bool = False,
    timeout_seconds: float = 90.0,
    poll_interval_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the read-only platform handoff and return a secret-safe report."""

    report: dict[str, Any] = {
        "mode": "platform_live_probe",
        "status": "NOT_READY",
        "remote_wordpress_writes": False,
        "paid_provider_requests": False,
        "checks": [],
        "jobs": [],
    }
    try:
        site, sites = client.read_site(site_id)
        selected_id = site["id"]
        report["site"] = _site_summary(site)
        report["checks"].append(_check("platform_authentication", "pass", "Authenticated platform session is active"))
        report["checks"].append(_check("site_access", "pass", "The selected site is visible to this team"))
        report["checks"].append(_check("site_selection", "pass", f"Selected one of {len(sites)} accessible site(s)"))

        overview = client.request("GET", f"/sites/{selected_id}/overview")
        if not isinstance(overview, dict):
            raise ProbeError("site overview response was not an object")
        settings = client.request("GET", "/settings")
        if not isinstance(settings, dict):
            raise ProbeError("workspace controls response was not an object")
        connections = overview.get("connections", [])
        if not isinstance(connections, list):
            connections = []
        wordpress = next((item for item in connections if isinstance(item, dict) and item.get("kind") == "wordpress"), None)
        if wordpress is None or wordpress.get("status") in {None, "needs_connection", "revoked"}:
            report["checks"].append(_check("wordpress_connection", "needs_connection", "Connect and test WordPress before live-site verification"))
        elif wordpress.get("status") != "connected":
            report["checks"].append(_check("wordpress_connection", "needs_review", "WordPress is configured but has not reached a verified connected state"))
        else:
            capabilities = wordpress.get("capabilities") if isinstance(wordpress.get("capabilities"), dict) else {}
            report["checks"].append(
                _check(
                    "wordpress_connection",
                    "pass",
                    "WordPress connection is marked connected by the platform",
                    authenticated=capabilities.get("authenticated") is True,
                    native=capabilities.get("native") if isinstance(capabilities.get("native"), dict) else {},
                )
            )

        global_pause = settings.get("global_pause") is True
        site_paused = site.get("paused") is True
        if global_pause and site_paused:
            pause_status = "pass"
            pause_detail = "Global and site pauses are active; live automation remains held"
        elif global_pause or site_paused:
            pause_status = "needs_review"
            pause_detail = "One pause control is active; verify the intended pilot policy before proceeding"
        else:
            pause_status = "needs_review"
            pause_detail = "Pause controls are inactive; do not use this probe to authorize live publishing"
        report["checks"].append(_check("pause_controls", pause_status, pause_detail, global_pause=global_pause, site_paused=site_paused))

        coverage = overview.get("coverage") if isinstance(overview.get("coverage"), dict) else {}
        report["checks"].append(
            _check(
                "audit_coverage",
                "pass" if coverage.get("status") == "complete" else "needs_review",
                "The latest platform audit coverage is reported without treating it as an optimization score",
                coverage_status=coverage.get("status", "unknown"),
                error_count=_safe_int(coverage.get("error_count")) or 0,
                pending_url_count=_safe_int(coverage.get("pending_url_count")) or 0,
            )
        )

        for kind, requested in (("inventory", run_inventory), ("audit", run_audit)):
            if not requested:
                continue
            queued = client.queue_read_only_job(selected_id, kind)
            job_id = queued.get("id") if isinstance(queued, dict) else None
            if not isinstance(job_id, str):
                raise ProbeError(f"the platform did not return a job id for {kind}")
            deadline = time.monotonic() + max(0.1, timeout_seconds)
            job = queued
            while isinstance(job, dict) and job.get("status") not in TERMINAL_JOB_STATUSES:
                if time.monotonic() >= deadline:
                    report["jobs"].append({"kind": kind, "job_id": job_id, "status": "timeout"})
                    report["checks"].append(_check(f"{kind}_job", "needs_review", f"The {kind} job did not finish before the probe timeout"))
                    break
                sleep(max(0.05, poll_interval_seconds))
                job = client.get_job(selected_id, job_id)
            else:
                summary = summarize_job(job if isinstance(job, dict) else {}, kind=kind)
                report["jobs"].append(summary)
                job_status = summary.get("status")
                report["checks"].append(
                    _check(
                        f"{kind}_job",
                        "pass" if job_status in {"complete", "partial"} else "needs_review",
                        f"Read-only {kind} job finished with status {job_status}",
                    )
                )

        blocking = {"fail", "needs_connection"}
        check_statuses = {check.get("status") for check in report["checks"]}
        report["status"] = "READ_ONLY_CHECK_PASSED" if not check_statuses.intersection(blocking) else "NOT_READY"
        return report
    except ProbeError as exc:
        report["checks"].append(_check("probe", "fail", str(exc)))
        report["status"] = "NOT_READY"
        return report
    except (httpx.HTTPError, OSError) as exc:
        report["checks"].append(_check("probe", "fail", f"Deployment could not be contacted ({type(exc).__name__})"))
        report["status"] = "NOT_READY"
        return report


def _render(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a read-only ForgeSEO platform live-site handoff probe")
    parser.add_argument("--base-url", default=os.environ.get("FORGE_PLATFORM_URL"), help="Standalone deployment origin (or FORGE_PLATFORM_URL)")
    parser.add_argument("--site-id", default=os.environ.get("FORGE_PLATFORM_SITE_ID"), help="Site id (or FORGE_PLATFORM_SITE_ID)")
    parser.add_argument("--run-inventory", action="store_true", help="Queue and wait for a read-only WordPress inventory job")
    parser.add_argument("--run-audit", action="store_true", help="Queue and wait for a public, read-only audit job")
    parser.add_argument("--timeout", type=float, default=90.0, help="Maximum seconds to wait for each requested job")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Seconds between job status reads")
    parser.add_argument("--output", type=Path, help="Optional path for the JSON report")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of the compact human summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    email = os.environ.get("FORGE_PLATFORM_EMAIL", "").strip()
    password = os.environ.get("FORGE_PLATFORM_PASSWORD", "")
    try:
        origin = normalize_origin(args.base_url or "")
        if not email or not password:
            raise ProbeError("FORGE_PLATFORM_EMAIL and FORGE_PLATFORM_PASSWORD are required; credentials are never accepted as CLI arguments")
        with httpx.Client(base_url=f"{origin}/api/v1", follow_redirects=False, timeout=15.0) as http_client:
            platform = PlatformClient(http_client, origin)
            platform.login(email, password)
            report = run_probe(
                platform,
                site_id=args.site_id,
                run_inventory=args.run_inventory,
                run_audit=args.run_audit,
                timeout_seconds=args.timeout,
                poll_interval_seconds=args.poll_interval,
            )
    except ProbeError as exc:
        report = {
            "mode": "platform_live_probe",
            "status": "NOT_READY",
            "remote_wordpress_writes": False,
            "paid_provider_requests": False,
            "checks": [_check("probe", "fail", str(exc))],
            "jobs": [],
        }
    rendered = _render(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    if args.json:
        sys.stdout.write(rendered)
    else:
        print(f"ForgeSEO platform probe: {report['status']}")
        for check in report.get("checks", []):
            print(f"- {check['name']}: {check['status']} — {check['detail']}")
        if report.get("jobs"):
            print(f"Read-only jobs: {len(report['jobs'])}")
        print("Remote WordPress writes: none")
    return 0 if report["status"] == "READ_ONLY_CHECK_PASSED" else 1


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
