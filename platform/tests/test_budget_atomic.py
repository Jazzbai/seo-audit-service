from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.budgets import release, reserve
from app.models import Base, Site, Team


@pytest.fixture()
def budget_database(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'budget.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    with factory() as db:
        team = Team(name="Budget test team")
        db.add(team)
        db.flush()
        site = Site(team_id=team.id, name="Budget test site", origin="https://example.test")
        db.add(site)
        db.commit()
        site_id = site.id
    try:
        yield factory, site_id
    finally:
        engine.dispose()


def test_first_reservation_is_atomic_when_callers_have_an_active_transaction(budget_database):
    factory, site_id = budget_database
    barrier = Barrier(2)

    def attempt(number: int) -> str:
        with factory() as db:
            # This mirrors the normal workflow's policy/site read before the
            # reservation call and deliberately leaves the session active.
            assert db.scalar(select(Site).where(Site.id == site_id)) is not None
            barrier.wait(timeout=10)
            try:
                reserve(db, site_id, f"active-transaction-{number}", 60, limit_cents=100)
                return "reserved"
            except ValueError as exc:
                assert str(exc) == "monthly budget exceeded"
                return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, range(2)))

    assert outcomes == ["denied", "reserved"]


def test_existing_account_reservation_is_atomic_when_callers_have_an_active_transaction(budget_database):
    factory, site_id = budget_database
    with factory() as db:
        seed = reserve(db, site_id, "seed-account", 0, limit_cents=100)
        release(db, seed.id)

    barrier = Barrier(2)

    def attempt(number: int) -> str:
        with factory() as db:
            assert db.scalar(select(Site).where(Site.id == site_id)) is not None
            barrier.wait(timeout=10)
            try:
                reserve(db, site_id, f"existing-active-transaction-{number}", 60, limit_cents=100)
                return "reserved"
            except ValueError as exc:
                assert str(exc) == "monthly budget exceeded"
                return "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = sorted(pool.map(attempt, range(2)))

    assert outcomes == ["denied", "reserved"]
