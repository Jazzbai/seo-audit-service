"""Deterministic seven-day unattended-pilot rehearsal.

This module is intentionally a simulation, not the unattended-pilot gate.  It
uses a disposable SQLite database, a fixed virtual clock, and local stubs for
broker delivery and one read-only worker handler.  It never contacts a
WordPress site, a broker, a paid provider, or an AI provider, and it must not
be used as evidence that the real seven-day acceptance gate passed.

The rehearsal is kept in a script rather than production code so that the
contracts it exercises remain the source of truth.  A contract change that
breaks one of the checks should fail this command/test instead of being
silently accommodated here.
"""

from __future__ import annotations

import argparse
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import operations, scheduler, worker, workflows
from app.config import settings
from app.models import (
    Article,
    Base,
    Connection,
    Event,
    Incident,
    Job,
    Page,
    Site,
    Team,
)
from app.operations import enqueue
from app.policies import create_policy


START = datetime(2026, 9, 21, 16, 0)
SIMULATED_DAYS = 7
TICK_HOURS = 24
SITE_TIMEZONE = "America/Chicago"
SITE_ORIGIN = "https://rehearsal.example.test"


class VirtualClock:
    """A deliberately small clock whose value is controlled by the fixture."""

    def __init__(self, value: datetime = START) -> None:
        self.value = value.replace(tzinfo=None)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **delta: int) -> None:
        self.value += timedelta(**delta)


@contextmanager
def disposable_database() -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    """Create and tear down the only database used by the rehearsal."""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _seed(factory: sessionmaker[Session], clock: VirtualClock) -> dict[str, str]:
    """Seed only local fixture records; credentials are never needed."""

    with factory() as db:
        team = Team(name="Seven-day rehearsal team", created_at=clock())
        db.add(team)
        db.flush()
        site = Site(
            team_id=team.id,
            name="Seven-day rehearsal site",
            origin=SITE_ORIGIN,
            timezone=SITE_TIMEZONE,
            language="en",
            facts={
                "business_name": "Rehearsal Business",
                "audience": "local customers",
                "services": ["Service example"],
                "locations": ["Houston"],
                "authors": [{"id": "fixture-author", "name": "Fixture Author"}],
            },
            paused=False,
            created_at=clock(),
        )
        db.add(site)
        db.flush()
        db.add(Connection(
            site_id=site.id,
            kind="wordpress",
            # This is an inert marker in an isolated database.  It is never
            # read as a credential and is deliberately excluded from output.
            encrypted_credentials="fixture-only",
            status="connected",
            capabilities={"authenticated": True, "inventory": True},
            checked_at=clock(),
            created_at=clock(),
        ))
        policy = create_policy(
            db,
            site,
            None,
            {
                "enabled": True,
                "allowed_actions": ["publish"],
                "protected_paths": ["/", "/contact*"],
                "posts_per_week": 2,
                "publish_days": [0],
                "author_id": "fixture-author",
            },
        )

        articles: list[Article] = []
        for index in range(1, 4):
            article = Article(
                site_id=site.id,
                title=f"Rehearsal article {index}",
                slug=f"rehearsal-article-{index}",
                body=f"<p>Deterministic rehearsal article {index}.</p>",
                status="checked",
                brief={"purpose": "new_article"},
                checks={"passed": True},
                sources=[],
                author_id="fixture-author",
                managed=True,
                created_at=clock(),
                updated_at=clock(),
            )
            articles.append(article)
            db.add(article)

        protected_page = Page(
            site_id=site.id,
            resource_key="pages:contact",
            url=f"{SITE_ORIGIN}/contact",
            title="Contact",
            resource_type="pages",
            source_hash="fixture-contact-source",
            enrolled=False,
            managed=False,
            created_at=clock(),
        )
        db.add(protected_page)
        db.flush()

        interrupted = Job(
            site_id=site.id,
            kind="availability",
            status="running",
            payload={"rehearsal": True},
            result={},
            idempotency_key="rehearsal:stale-lease",
            attempts=1,
            available_at=clock() - timedelta(minutes=1),
            lease_until=clock() - timedelta(seconds=1),
            created_at=clock() - timedelta(minutes=2),
            updated_at=clock() - timedelta(minutes=2),
        )
        db.add(interrupted)

        # This second site is an isolated scheduler fixture.  Its connection
        # rows contain capability evidence only: the marker values are not
        # credentials, are never decrypted, and are never passed to a client.
        autopilot_site = Site(
            team_id=team.id,
            name="Content-autopilot rehearsal site",
            origin="https://autopilot.rehearsal.example.test",
            timezone=SITE_TIMEZONE,
            language="en",
            facts={
                "business_name": "Autopilot Rehearsal Business",
                "audience": "local customers",
                "services": ["Fixture service"],
                "locations": ["Houston"],
                "authors": [{"id": "autopilot-fixture-author", "name": "Fixture Author"}],
            },
            paused=False,
            created_at=clock(),
        )
        db.add(autopilot_site)
        db.flush()
        db.add_all([
            Connection(
                site_id=autopilot_site.id,
                kind="wordpress",
                encrypted_credentials="local-fixture-wordpress-marker",
                status="connected",
                capabilities={
                    "authenticated": True,
                    "native": {"create": True, "publish": True},
                },
                checked_at=clock(),
                created_at=clock(),
            ),
            Connection(
                site_id=autopilot_site.id,
                kind="ai",
                encrypted_credentials="local-fixture-ai-marker",
                status="connected",
                capabilities={
                    "settings": {
                        "endpoint": "https://ai.fixture.invalid/generate",
                        "model": "fixture-model",
                        "estimated_cost_cents": 1,
                        "max_cost_cents": 2,
                    },
                },
                checked_at=clock(),
                created_at=clock(),
            ),
        ])
        create_policy(
            db,
            autopilot_site,
            None,
            {
                "enabled": True,
                "allowed_actions": ["publish"],
                "protected_paths": ["/", "/contact*"],
                "posts_per_week": 1,
                "publish_days": [0, 4],
                "author_id": "autopilot-fixture-author",
            },
        )
        for index in range(1, 3):
            db.add(Article(
                site_id=autopilot_site.id,
                title=f"Autopilot rehearsal article {index}",
                slug=f"autopilot-rehearsal-article-{index}",
                body=f"<p>Disposable autopilot fixture article {index}.</p>",
                status="planned",
                brief={"purpose": "new_article"},
                checks={},
                sources=[],
                author_id="autopilot-fixture-author",
                managed=True,
                created_at=clock(),
                updated_at=clock(),
            ))
        db.commit()

        return {
            "site_id": site.id,
            "autopilot_site_id": autopilot_site.id,
            "policy_version": str(policy.version),
            "article_one": articles[0].id,
            "article_three": articles[2].id,
            "protected_page": protected_page.id,
            "interrupted_job": interrupted.id,
        }


def _scheduled_slot_counts(
    db: Session, site_id: str, kinds: tuple[str, ...]
) -> dict[str, int]:
    """Count only scheduler-created idempotency slots, not fixture jobs."""

    rows = db.scalars(select(Job).where(Job.site_id == site_id)).all()
    return {
        kind: sum(
            1
            for row in rows
            if row.kind == kind and f":schedule:{kind}:" in row.idempotency_key
        )
        for kind in kinds
    }


def _content_autopilot_jobs(db: Session, site_id: str) -> list[Job]:
    """Return only the disposable site's durable autopilot parents."""

    return db.scalars(select(Job).where(
        Job.site_id == site_id,
        Job.kind == "content_autopilot",
    ).order_by(Job.created_at, Job.id)).all()


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


@contextmanager
def _simulation_runtime(
    factory: sessionmaker[Session],
    engine: Engine,
    clock: VirtualClock,
    dispatches: list[dict[str, Any]],
) -> Iterator[None]:
    """Route every scheduler/worker dependency to the disposable runtime."""

    def local_dispatch(*args: Any, **kwargs: Any) -> None:
        # The real Celery publish operation is intentionally replaced.  The
        # recorded shape is useful evidence that scheduling reached the local
        # dispatch boundary, while no broker connection can occur.
        dispatches.append({"args": list(args), "queue": kwargs.get("queue")})

    async def read_only_fixture_handler(
        db: Session, site: Site, job: Job
    ) -> dict[str, Any]:
        return {
            "complete": True,
            "rehearsal": True,
            "provider_contacted": False,
            "virtual_observed_at": clock().isoformat() + "Z",
        }

    with (
        patch.object(settings, "GLOBAL_PAUSE", False),
        patch.object(scheduler, "SessionLocal", factory),
        patch.object(scheduler, "now", clock),
        patch.object(worker, "SessionLocal", factory),
        patch.object(worker, "engine", engine),
        patch.object(worker, "now", clock),
        patch.object(operations, "now", clock),
        patch.object(worker.execute_job, "apply_async", side_effect=local_dispatch),
        patch.dict(workflows.HANDLERS, {"availability": read_only_fixture_handler}),
    ):
        yield


def run_rehearsal() -> dict[str, Any]:
    """Run the complete local simulation and return secret-safe evidence.

    The returned status is deliberately named ``REHEARSAL_PASS``.  It is not
    a production readiness result and never uses the real seven-day gate's
    ``PASS`` vocabulary.
    """

    clock = VirtualClock()
    dispatches: list[dict[str, Any]] = []
    cadence_kinds = (
        "availability",
        "poll_changes",
        "inventory",
        "audit",
        "plan",
        "refresh",
    )
    autopilot_initial_parent_count = 0
    autopilot_same_window_duplicate_parents = 0
    autopilot_active_reservation_holds = 0
    autopilot_weekly_quota_holds = 0
    autopilot_uncertain_outcome_holds = 0
    autopilot_reconciled_parent_count = 0
    autopilot_parents_after_reconciliation = 0

    with disposable_database() as (engine, factory):
        ids = _seed(factory, clock)
        with _simulation_runtime(factory, engine, clock, dispatches):
            # Tick at the start and at each daily boundary.  This is an
            # exactly seven-day virtual window, including both endpoints.
            for day in range(SIMULATED_DAYS + 1):
                if day:
                    clock.advance(days=1)

                # The first parent is held as an uncertain local outcome after
                # the Friday window.  Reconciliation is represented only by a
                # local row update before the next Monday window; it cannot
                # perform a remote retry or infer that a write happened.
                if day == SIMULATED_DAYS:
                    with factory() as db:
                        parents = _content_autopilot_jobs(
                            db, ids["autopilot_site_id"]
                        )
                        _assert(
                            len(parents) == 1,
                            "rehearsal lost the uncertain autopilot parent before reconciliation",
                        )
                        parent = parents[0]
                        _assert(
                            parent.status == "needs_reconciliation"
                            and parent.result.get("status") == "needs_reconciliation",
                            "autopilot uncertainty fixture was not retained",
                        )
                        parent.status = "complete"
                        parent.result = {
                            "status": "reconciled",
                            "remote_outcome": "resolved_locally",
                        }
                        parent.lease_until = None
                        parent.updated_at = clock()
                        db.commit()
                        autopilot_reconciled_parent_count += 1

                scheduler.schedule()

                if day == 0:
                    # Monday is the first local publish window.  A second
                    # tick in the same window must reuse the durable daily
                    # key rather than create another parent.
                    with factory() as db:
                        parents = _content_autopilot_jobs(
                            db, ids["autopilot_site_id"]
                        )
                        _assert(
                            len(parents) == 1,
                            "eligible autopilot window did not queue exactly one parent",
                        )
                        autopilot_initial_parent_count = len(parents)
                    scheduler.schedule()
                    with factory() as db:
                        parents = _content_autopilot_jobs(
                            db, ids["autopilot_site_id"]
                        )
                        autopilot_same_window_duplicate_parents = max(
                            0, len(parents) - autopilot_initial_parent_count
                        )
                        _assert(
                            len(parents) == autopilot_initial_parent_count,
                            "repeated autopilot scheduler tick created a duplicate parent",
                        )

                if day == 4:
                    # The first parent is still active on Friday.  With a
                    # one-post weekly policy, its durable reservation fills
                    # the quota and prevents another automatic write.
                    with factory() as db:
                        parents = _content_autopilot_jobs(
                            db, ids["autopilot_site_id"]
                        )
                        autopilot_publish_jobs = db.scalars(select(Job).where(
                            Job.site_id == ids["autopilot_site_id"],
                            Job.kind == "publish",
                        )).all()
                        _assert(
                            len(parents) == 1
                            and parents[0].status in {"queued", "running", "retry"},
                            "active autopilot parent was not retained as a reservation",
                        )
                        _assert(
                            not autopilot_publish_jobs,
                            "autopilot quota fixture created an extra publish job",
                        )
                        autopilot_active_reservation_holds += 1
                        autopilot_weekly_quota_holds += 1

                        parent = parents[0]
                        parent.status = "needs_reconciliation"
                        parent.result = {
                            "status": "needs_reconciliation",
                            "remote_outcome": "unknown",
                        }
                        parent.lease_until = None
                        parent.updated_at = clock()
                        db.commit()

                    # An eligible tick after an uncertain outcome must hold the
                    # automatic path.  No provider or broker is involved.
                    scheduler.schedule()
                    with factory() as db:
                        parents = _content_autopilot_jobs(
                            db, ids["autopilot_site_id"]
                        )
                        _assert(
                            len(parents) == 1,
                            "uncertain autopilot outcome did not hold a later window",
                        )
                        autopilot_uncertain_outcome_holds += 1

                if day == SIMULATED_DAYS:
                    # Once the local fixture marks the outcome reconciled, the
                    # next eligible local window may reserve its next bounded
                    # write.  A repeated tick still cannot duplicate it.
                    with factory() as db:
                        parents = _content_autopilot_jobs(
                            db, ids["autopilot_site_id"]
                        )
                        _assert(
                            len(parents) == 2,
                            "reconciled autopilot parent did not permit the next window",
                        )
                        autopilot_parents_after_reconciliation = (
                            len(parents) - autopilot_initial_parent_count
                        )
                    scheduler.schedule()
                    with factory() as db:
                        _assert(
                            len(_content_autopilot_jobs(
                                db, ids["autopilot_site_id"]
                            )) == 2,
                            "repeated post-reconciliation tick created a duplicate parent",
                        )

                # The stale lease is handled on the first tick.  Immediately
                # exercise the worker's real claim/finalize path with a local
                # read-only handler, before the next virtual tick.
                if day == 0:
                    with factory() as db:
                        recovered = db.get(Job, ids["interrupted_job"])
                        _assert(recovered is not None, "stale fixture job disappeared")
                        _assert(
                            recovered.status == "retry",
                            "expired read-only worker lease did not become retryable",
                        )
                        interrupted_event = db.scalar(select(Event).where(
                            Event.site_id == ids["site_id"],
                            Event.kind == "worker_interrupted",
                        ))
                        _assert(
                            interrupted_event is not None,
                            "worker interruption was not made visible as an event",
                        )
                    worker.run_job(ids["interrupted_job"])

            with factory() as db:
                site = db.get(Site, ids["site_id"])
                _assert(site is not None, "rehearsal site disappeared")

                slots = _scheduled_slot_counts(db, site.id, cadence_kinds)
                # Eight observations occur over the endpoint-inclusive window;
                # weekly jobs may cross one local-week boundary, but never
                # multiply within a bucket.
                slot_limits = {
                    "availability": SIMULATED_DAYS + 1,
                    "poll_changes": SIMULATED_DAYS + 1,
                    "inventory": SIMULATED_DAYS + 1,
                    "audit": 2,
                    "plan": 2,
                    "refresh": 2,
                }
                _assert(
                    all(slots[kind] <= slot_limits[kind] for kind in cadence_kinds),
                    f"cadence slot bound exceeded: {slots}",
                )

                automatic_publish_jobs = [
                    row
                    for row in db.scalars(select(Job).where(
                        Job.site_id == site.id,
                        Job.kind == "publish",
                    )).all()
                    if "rehearsal:paused-write" not in row.idempotency_key
                ]
                _assert(
                    len(automatic_publish_jobs) <= 2,
                    "scheduler created more automatic publication jobs than policy quota",
                )
                deferred = db.scalars(select(Event).where(
                    Event.site_id == site.id,
                    Event.kind == "publication_deferred_quota",
                )).all()
                _assert(deferred, "full publication queue did not remain visibly deferred")
                _assert(
                    all(
                        isinstance(item.data, dict)
                        and item.data.get("limit") == 2
                        and item.data.get("reason") == "posts_per_week"
                        for item in deferred
                    ),
                    "publication deferral evidence did not preserve the quota reason",
                )

                recovered = db.get(Job, ids["interrupted_job"])
                _assert(
                    recovered is not None and recovered.status == "complete",
                    "stale read-only worker lease did not recover to complete",
                )
                recovery_events = db.scalars(select(Event).where(
                    Event.site_id == site.id,
                    Event.kind == "worker_interrupted",
                )).all()
                _assert(len(recovery_events) == 1, "worker interruption evidence was duplicated")

                # Queue a write while the site is paused.  ``enqueue`` is the
                # real durable queue helper; the scheduler must leave this
                # row undelivered and the policy guard must also reject it.
                site.paused = True
                paused_job = enqueue(
                    db,
                    site,
                    "publish",
                    {"article_id": ids["article_three"]},
                    "rehearsal:paused-write",
                )
                db.refresh(paused_job)
                _assert(
                    paused_job.status == "queued" and paused_job.lease_until is None,
                    "paused publication was not held in the durable queue",
                )
                before_dispatch_count = len(dispatches)

            # The pause test intentionally invokes another real scheduler
            # tick.  Read-only cadence work may dispatch through the local
            # stub, but the paused publication must not cross that boundary.
            scheduler.schedule()

            with factory() as db:
                paused_job = db.scalar(select(Job).where(
                    Job.site_id == ids["site_id"],
                    Job.idempotency_key.like("%rehearsal:paused-write"),
                ))
                _assert(paused_job is not None, "paused publication row disappeared")
                _assert(
                    paused_job.status == "queued" and paused_job.lease_until is None,
                    "scheduler delivered a pause-held publication",
                )
                _assert(
                    len(dispatches) >= before_dispatch_count,
                    "local dispatch accounting moved backwards",
                )

                protected_page = db.get(Page, ids["protected_page"])
                site = db.get(Site, ids["site_id"])
                _assert(protected_page is not None and site is not None, "guard fixture disappeared")

                site.paused = False
                db.flush()
                protected_reasons: list[str] = []
                try:
                    workflows.authorize(db, site, "publish", page=protected_page)
                except ValueError as exc:
                    protected_reasons = str(exc).split(": ", 1)[-1].split(", ")
                _assert(
                    "protected_path" in protected_reasons,
                    "protected page did not remain blocked by the policy guard",
                )

                protected_page.url = f"{SITE_ORIGIN}/draft"
                site.paused = True
                db.flush()
                pause_reasons: list[str] = []
                try:
                    workflows.authorize(db, site, "publish", page=protected_page)
                except ValueError as exc:
                    pause_reasons = str(exc).split(": ", 1)[-1].split(", ")
                _assert(
                    "site_paused" in pause_reasons,
                    "paused site did not remain blocked by the policy guard",
                )
                db.rollback()

        # Only scalar, non-sensitive evidence leaves the disposable database.
        with factory() as db:
            final_site = db.get(Site, ids["site_id"])
            final_slots = _scheduled_slot_counts(db, ids["site_id"], cadence_kinds)
            automatic_publish_count = sum(
                1
                for row in db.scalars(select(Job).where(
                    Job.site_id == ids["site_id"],
                    Job.kind == "publish",
                )).all()
                if "rehearsal:paused-write" not in row.idempotency_key
            )
            held_publish_count = len(db.scalars(select(Job).where(
                Job.site_id == ids["site_id"],
                Job.kind == "publish",
                Job.idempotency_key.like("%rehearsal:paused-write"),
            )).all())
            final_interrupted = db.get(Job, ids["interrupted_job"])
            worker_event_count = len(db.scalars(select(Event).where(
                Event.site_id == ids["site_id"], Event.kind == "worker_interrupted"
            )).all())
            deferred_count = len(db.scalars(select(Event).where(
                Event.site_id == ids["site_id"], Event.kind == "publication_deferred_quota"
            )).all())
            autopilot_parent_count = len(_content_autopilot_jobs(
                db, ids["autopilot_site_id"]
            ))
            autopilot_automatic_publish_count = len(db.scalars(select(Job).where(
                Job.site_id == ids["autopilot_site_id"],
                Job.kind == "publish",
            )).all())
            incident_count = len(db.scalars(select(Incident).where(
                Incident.site_id == ids["site_id"]
            )).all())
            _assert(final_site is not None and final_site.paused is True, "pause state was not retained")
            _assert(final_interrupted is not None and final_interrupted.status == "complete", "recovery lost")
            _assert(autopilot_parent_count == 2, "autopilot parent count was not bounded")
            _assert(autopilot_automatic_publish_count == 0, "autopilot created an unexpected publish job")

    return {
        "status": "REHEARSAL_PASS",
        "mode": "seven_day_unattended_pilot_rehearsal",
        "simulation": True,
        "real_seven_day_acceptance_gate": "NOT_RUN",
        "network_access": "disabled_for_fixture",
        "database": "disposable_sqlite_memory",
        "virtual_window": {
            "start_utc": START.isoformat() + "Z",
            "end_utc": (START + timedelta(days=SIMULATED_DAYS)).isoformat() + "Z",
            "ticks": SIMULATED_DAYS + 2,
        },
        "checks": {
            "daily_weekly_cadence": {
                "status": "verified_in_simulation",
                "scheduled_slots": final_slots,
                "limits": {
                    "availability": SIMULATED_DAYS + 1,
                    "poll_changes": SIMULATED_DAYS + 1,
                    "inventory": SIMULATED_DAYS + 1,
                    "audit": 2,
                    "plan": 2,
                    "refresh": 2,
                },
            },
            "publication_quota": {
                "status": "verified_in_simulation",
                "policy_limit_per_local_week": 2,
                "automatic_publish_jobs": automatic_publish_count,
                "deferred_quota_events": deferred_count,
                "published_remotely": 0,
            },
            "automatic_content_scheduling": {
                "status": "verified_in_simulation",
                "policy_limit_per_local_week": 1,
                "initial_window_parent_count": autopilot_initial_parent_count,
                "same_window_duplicate_parents": autopilot_same_window_duplicate_parents,
                "active_parent_reservation_holds": autopilot_active_reservation_holds,
                "weekly_quota_holds": autopilot_weekly_quota_holds,
                "uncertain_outcome_holds": autopilot_uncertain_outcome_holds,
                "reconciled_parent_count": autopilot_reconciled_parent_count,
                "parents_queued_after_reconciliation": autopilot_parents_after_reconciliation,
                "parent_count_total": autopilot_parent_count,
                "automatic_publish_jobs": autopilot_automatic_publish_count,
                "published_remotely": 0,
            },
            "worker_interruption_recovery": {
                "status": "verified_in_simulation",
                "interruption_events": worker_event_count,
                "recovered_job_status": final_interrupted.status,
                "remote_outcome": "not_contacted",
            },
            "pause_and_protected_write_guards": {
                "status": "verified_in_simulation",
                "site_paused_after_rehearsal": final_site.paused,
                "pause_held_publish_jobs": held_publish_count,
                "protected_path_blocked": True,
                "paused_site_blocked": True,
            },
        },
        "local_dispatch_stub_calls": len(dispatches),
        "limitations": [
            "No real WordPress, broker, browser, AI, search, analytics, or paid-provider connection was exercised.",
            "No article was remotely published; publication verification and rollback remain external integration gates.",
            "This is not a production acceptance result and does not certify the real seven-day unattended pilot.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the ForgeSEO seven-day pilot rehearsal (simulation only)."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print secret-safe machine-readable rehearsal evidence",
    )
    args = parser.parse_args(argv)
    try:
        report = run_rehearsal()
    except Exception as exc:
        # Avoid emitting exception text: even a future fixture/provider error
        # must not accidentally echo a credential-shaped value.
        report = {
            "status": "REHEARSAL_FAIL",
            "mode": "seven_day_unattended_pilot_rehearsal",
            "simulation": True,
            "real_seven_day_acceptance_gate": "NOT_RUN",
            "detail": type(exc).__name__,
            "limitations": [
                "The rehearsal failed before producing complete simulation evidence.",
                "This result does not certify the real seven-day unattended pilot.",
            ],
        }
        if not args.json:
            print("ForgeSEO seven-day pilot rehearsal (SIMULATION ONLY)")
            print(f"Status: {report['status']}")
            print(f"Failure type: {report['detail']}")
            print("Real seven-day acceptance gate: NOT RUN")
        else:
            print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("ForgeSEO seven-day pilot rehearsal (SIMULATION ONLY)")
        print(f"Status: {report['status']}")
        print("Real seven-day acceptance gate: NOT RUN")
        for name, check in report["checks"].items():
            print(f"- {name}: {check['status']}")
        print("No external providers or live site were contacted.")
        print("This output is rehearsal evidence, not a production acceptance result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
