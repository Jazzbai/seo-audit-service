r"""Opt-in PostgreSQL integration tests for the isolated named fixture.

The test module may start only ``deploy/compose.postgres-test.yaml`` under its
unique Compose project.  It never reads the application DATABASE_URL and it
rejects any PostgreSQL URL that is not loopback port 15434.

PowerShell:

    $env:FORGE_LIVE_PG = "1"
    .\.venv\Scripts\python.exe -m pytest -q tests/test_postgres_live.py

The fixture is disposable at the database level: every test gets a freshly
migrated database, and the named Docker volume is retained for the fixture
itself.  Credentials are held in process memory and all subprocess output is
discarded rather than logged.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from typing import Iterator
from uuid import uuid4
from zipfile import ZipFile

import pytest
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.budgets import reserve
from app.models import (
    Base,
    BudgetAccount,
    Candidate,
    Connection,
    CostReservation,
    Heartbeat,
    Job,
    Membership,
    Page,
    Policy,
    Session as AuthSession,
    Site,
    Team,
    User,
    utcnow,
)
from app.policies import create_policy


pytestmark = pytest.mark.skipif(
    os.environ.get("FORGE_LIVE_PG") != "1",
    reason="Explicit isolated PostgreSQL integration opt-in required",
)

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "compose.postgres-test.yaml"
COMPOSE_PROJECT = "forgeseo-postgres-live-test"
DEFAULT_PASSWORD = "forge-live-postgres-test-only"
DEFAULT_USER = "forge_live_test"
DEFAULT_DATABASE = "forge_live_fixture"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DATABASE_NAME_PATTERN = re.compile(r"^forge_live_[0-9a-f]{32}$")
EXPECTED_ALEMBIC_HEAD = "0002_session_team_scope"


class PostgresFixture:
    """A redacted holder for the explicitly allowed fixture connection."""

    def __init__(self, url: URL) -> None:
        self.url = url
        self.admin_url = url.set(database="postgres")

    def database_url(self, database: str) -> URL:
        if not DATABASE_NAME_PATTERN.fullmatch(database):
            raise ValueError("fixture database name was not generated locally")
        return self.url.set(database=database)

    def __repr__(self) -> str:
        return "<isolated PostgreSQL fixture credentials redacted>"


class PostgresDatabase:
    """A per-test migrated database with independently pooled sessions."""

    def __init__(self, name: str, url: URL, engine: Engine) -> None:
        self.name = name
        self.url = url
        self.engine = engine
        self.factory = sessionmaker(
            bind=engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )

    def __repr__(self) -> str:
        return "<isolated PostgreSQL test database credentials redacted>"


def _compose(*arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        COMPOSE_PROJECT,
        "--file",
        str(COMPOSE_FILE),
        *arguments,
    ]


def _run_compose(*arguments: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run only the named fixture and keep Docker output out of test logs."""

    try:
        return subprocess.run(
            _compose(*arguments),
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(
            "The isolated PostgreSQL Compose fixture could not be controlled; "
            f"details withheld ({type(exc).__name__})"
        )


def _fixture_url() -> PostgresFixture:
    raw_url = os.environ.get("FORGE_LIVE_PG_URL", "").strip()
    try:
        url = (
            make_url(raw_url)
            if raw_url
            else URL.create(
                "postgresql+psycopg",
                username=DEFAULT_USER,
                password=os.environ.get("FORGE_LIVE_PG_PASSWORD", DEFAULT_PASSWORD),
                host="127.0.0.1",
                port=15434,
                database=DEFAULT_DATABASE,
            )
        )
    except (TypeError, ValueError) as exc:
        pytest.fail(f"FORGE_LIVE_PG_URL is invalid; details withheld ({type(exc).__name__})")

    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if url.get_backend_name() != "postgresql":
        pytest.fail("FORGE_LIVE_PG_URL must use PostgreSQL; no database connection was attempted")
    if url.host not in LOOPBACK_HOSTS or url.port != 15434:
        pytest.fail(
            "FORGE_LIVE_PG_URL must target loopback port 15434; "
            "the original port 5432 and all non-loopback hosts are forbidden"
        )
    if not url.username:
        pytest.fail("The isolated PostgreSQL fixture URL must include a database user")
    return PostgresFixture(url)


def _ensure_fixture_running(fixture: PostgresFixture) -> bool:
    """Start the named service if needed; return whether this test owns it."""

    running = _run_compose("ps", "--status", "running", "-q", "postgres")
    if running.returncode == 0 and running.stdout.strip():
        return False

    compose_environment = os.environ.copy()
    if fixture.url.password is not None:
        # Compose consumes this only for the isolated fixture.  It is never
        # echoed, asserted, or included in a failure message.
        compose_environment["FORGE_LIVE_PG_PASSWORD"] = fixture.url.password
    started = _run_compose(
        "up",
        "--detach",
        "--wait",
        "--remove-orphans",
        "postgres",
        environment=compose_environment,
    )
    if started.returncode:
        pytest.fail("The named PostgreSQL fixture failed to start; output withheld")
    return True


def _engine(url: URL) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=0,
    )


def _create_database(admin_url: URL, name: str) -> None:
    if not DATABASE_NAME_PATTERN.fullmatch(name):
        raise ValueError("fixture database name was not generated locally")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with admin_engine.connect() as connection:
            # The identifier is generated locally and checked above; PostgreSQL
            # does not accept a bind parameter in CREATE DATABASE's identifier.
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    finally:
        admin_engine.dispose()


def _drop_database(admin_url: URL, name: str) -> None:
    if not DATABASE_NAME_PATTERN.fullmatch(name):
        raise ValueError("fixture database name was not generated locally")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        admin_engine.dispose()


def _migrate(url: URL) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = url.render_as_string(hide_password=False)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        pytest.fail("Alembic migration failed for the isolated database; output withheld")


@pytest.fixture(scope="session")
def postgres_live() -> Iterator[PostgresFixture]:
    fixture = _fixture_url()
    owns_compose = _ensure_fixture_running(fixture)
    admin_engine = _engine(fixture.admin_url)
    try:
        with admin_engine.connect() as connection:
            if connection.scalar(text("SELECT 1")) != 1:
                pytest.fail("The isolated PostgreSQL fixture did not answer its health query")
    except Exception as exc:
        admin_engine.dispose()
        if owns_compose:
            _run_compose("down", "--remove-orphans")
        pytest.fail(f"The isolated PostgreSQL fixture was unreachable; details withheld ({type(exc).__name__})")
    try:
        yield fixture
    finally:
        admin_engine.dispose()
        if owns_compose:
            # Keep the dedicated named volume; only stop containers owned by
            # this unique project.  No other Compose project is addressed.
            _run_compose("down", "--remove-orphans")


@pytest.fixture()
def postgres_database(postgres_live: PostgresFixture) -> Iterator[PostgresDatabase]:
    name = f"forge_live_{uuid4().hex}"
    _create_database(postgres_live.admin_url, name)
    engine: Engine | None = None
    try:
        url = postgres_live.database_url(name)
        _migrate(url)
        engine = _engine(url)
        with engine.connect() as connection:
            if connection.scalar(text("SELECT 1")) != 1:
                pytest.fail("The isolated migrated PostgreSQL database did not answer its health query")
        yield PostgresDatabase(name, url, engine)
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(postgres_live.admin_url, name)


@contextmanager
def _temporary_database(fixture: PostgresFixture) -> Iterator[PostgresDatabase]:
    name = f"forge_live_{uuid4().hex}"
    _create_database(fixture.admin_url, name)
    engine: Engine | None = None
    try:
        url = fixture.database_url(name)
        _migrate(url)
        engine = _engine(url)
        yield PostgresDatabase(name, url, engine)
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(fixture.admin_url, name)


def _site(factory: sessionmaker[Session], *, paused: bool = True) -> str:
    with factory() as db:
        team = Team(name="PostgreSQL integration team")
        db.add(team)
        db.flush()
        site = Site(
            team_id=team.id,
            name="Isolated PostgreSQL site",
            origin="https://postgres.fixture.invalid",
            paused=paused,
            facts={"business_name": "Fixture Workshop", "services": ["repairs"]},
        )
        db.add(site)
        db.commit()
        return site.id


def _tamper_artifact(payload: bytes, backup_key: bytes, artifact_name: str) -> bytes:
    decrypted = Fernet(backup_key).decrypt(payload)
    output = io.BytesIO()
    with ZipFile(io.BytesIO(decrypted), "r") as source, ZipFile(output, "w") as target:
        for info in source.infolist():
            body = source.read(info.filename)
            if info.filename == f"artifacts/{artifact_name}":
                body = b"tampered isolated fixture artifact"
            target.writestr(info, body)
    return Fernet(backup_key).encrypt(output.getvalue())


def test_postgres_migrations_create_the_expected_schema(postgres_database: PostgresDatabase) -> None:
    assert postgres_database.engine.dialect.name == "postgresql"
    tables = set(inspect(postgres_database.engine).get_table_names())
    assert set(Base.metadata.tables).issubset(tables)
    with postgres_database.engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == EXPECTED_ALEMBIC_HEAD


def test_postgres_budget_limit_is_atomic_across_real_sessions(
    postgres_database: PostgresDatabase,
) -> None:
    site_id = _site(postgres_database.factory, paused=True)
    barrier = Barrier(2)

    def attempt(number: int) -> tuple[int, str]:
        with postgres_database.factory() as db:
            backend_pid = int(db.scalar(text("SELECT pg_backend_pid()")))
            # The production budget API takes its advisory lock only when a
            # fresh session is supplied.  Roll back the read-only probe so
            # this worker exercises that real-session path.
            db.rollback()
            barrier.wait(timeout=30)
            try:
                reserve(
                    db,
                    site_id,
                    f"postgres-budget-{number}-{uuid4().hex}",
                    60,
                    limit_cents=100,
                )
                return backend_pid, "reserved"
            except ValueError as exc:
                assert str(exc) == "monthly budget exceeded"
                return backend_pid, "denied"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))

    assert len({pid for pid, _status in outcomes}) == 2
    assert sorted(status for _pid, status in outcomes) == ["denied", "reserved"]
    with postgres_database.factory() as db:
        account = db.scalar(select(BudgetAccount).where(BudgetAccount.site_id == site_id))
        assert account is not None
        assert (account.reserved_cents, account.spent_cents) == (60, 0)
        assert db.scalar(
            select(CostReservation.id).where(CostReservation.site_id == site_id)
        ) is not None


def test_postgres_policy_versions_are_allocated_concurrently_without_duplicates(
    postgres_database: PostgresDatabase,
) -> None:
    site_id = _site(postgres_database.factory, paused=True)
    barrier = Barrier(2)

    def allocate(number: int) -> tuple[int, int]:
        with postgres_database.factory() as db:
            backend_pid = int(db.scalar(text("SELECT pg_backend_pid()")))
            site = db.get(Site, site_id)
            assert site is not None
            barrier.wait(timeout=30)
            policy = create_policy(
                db,
                site,
                None,
                {
                    "enabled": False,
                    "allowed_actions": ["metadata"],
                    "tracked_keywords": [f"fixture-{number}"],
                },
            )
            db.commit()
            return backend_pid, policy.version

    with ThreadPoolExecutor(max_workers=2) as pool:
        allocations = list(pool.map(allocate, range(2)))

    assert len({pid for pid, _version in allocations}) == 2
    assert sorted(version for _pid, version in allocations) == [1, 2]
    with postgres_database.factory() as db:
        versions = db.scalars(
            select(Policy.version)
            .where(Policy.site_id == site_id)
            .order_by(Policy.version)
        ).all()
        assert versions == [1, 2]


def test_postgres_candidate_execution_preserves_siblings_and_site_scope(
    postgres_database: PostgresDatabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A repeated execution cannot consume siblings or another site's rows."""

    import asyncio
    import copy
    from html import escape

    from fastapi import HTTPException

    from app.api import Decision, candidate_for_site, decide
    from app import workflows
    from app.config import settings

    site_a_id = _site(postgres_database.factory, paused=False)
    site_b_id = _site(postgres_database.factory, paused=False)
    states: dict[str, dict] = {}

    def initial_source(site_id: str, origin: str) -> dict:
        title = "Original fixture article"
        description = "Original fixture description with practical repair guidance for visitors."
        return {
            "resource_key": "posts:1",
            "resource_type": "posts",
            "url": f"{origin}/article",
            "public_url": f"{origin}/article",
            "title": title,
            "source_hash": f"source-{site_id}",
            "metadata": {
                "seo": {
                    "forgeseo": {
                        "title": title,
                        "description": description,
                    }
                }
            },
        }

    def render(source: dict) -> str:
        seo = source["metadata"]["seo"]["forgeseo"]
        title = escape(str(seo["title"]), quote=True)
        description = escape(str(seo["description"]), quote=True)
        url = escape(str(source["url"]), quote=True)
        return (
            "<!doctype html><html><head>"
            f"<title>{title}</title>"
            f'<meta name="description" content="{description}">'
            f'<meta property="og:title" content="{title}">'
            f'<meta property="og:description" content="{description}">'
            f'<link rel="canonical" href="{url}">'
            '<meta name="robots" content="index,follow">'
            "</head><body><main><h1>Fixture repair article</h1>"
            "<p>Practical repair guidance for visitors preparing for service.</p>"
            "</main></body></html>"
        )

    class FakeClient:
        def __init__(self, state: dict) -> None:
            self.state = state

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def read(self, resource_key: str) -> dict:
            assert resource_key == self.state["resource_key"]
            return copy.deepcopy(self.state)

        async def update(
            self,
            resource_key: str,
            payload: dict,
            expected_source_hash: str,
            **_kwargs,
        ) -> dict:
            assert resource_key == self.state["resource_key"]
            assert expected_source_hash == self.state["source_hash"]
            seo = payload["seo"]
            field, value = next(iter(seo.items()))
            updated = copy.deepcopy(self.state)
            updated["metadata"]["seo"]["forgeseo"][field] = value
            if field == "title":
                updated["title"] = value
            updated["source_hash"] = f"{self.state['source_hash']}-updated"
            self.state.clear()
            self.state.update(updated)
            return copy.deepcopy(self.state)

        async def restore(
            self,
            resource_key: str,
            before: dict,
            expected_source_hash: str,
            **_kwargs,
        ) -> dict:
            assert resource_key == self.state["resource_key"]
            assert expected_source_hash == self.state["source_hash"]
            self.state.clear()
            self.state.update(copy.deepcopy(before))
            return copy.deepcopy(self.state)

    async def fake_client_for(db, site, kind="wordpress"):
        del db
        assert kind == "wordpress"
        return FakeClient(states[site.id])

    async def fake_fetch(url: str) -> dict:
        for source in states.values():
            if source["url"] == url:
                return {"status_code": 200, "html": render(source)}
        raise AssertionError(f"fixture fetch escaped the site allowlist: {url}")

    monkeypatch.setattr(workflows, "client_for", fake_client_for)
    monkeypatch.setattr(workflows, "fetch", fake_fetch)
    monkeypatch.setattr(settings, "GLOBAL_PAUSE", False)
    monkeypatch.setattr(settings, "ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    with postgres_database.factory() as db:
        site_a = db.get(Site, site_a_id)
        site_b = db.get(Site, site_b_id)
        assert site_a is not None and site_b is not None
        site_a.origin = "https://postgres-a.fixture.invalid"
        site_b.origin = "https://postgres-b.fixture.invalid"

        for site in (site_a, site_b):
            create_policy(
                db,
                site,
                None,
                {"enabled": True, "allowed_actions": ["metadata"]},
            )
            db.add(
                Connection(
                    site_id=site.id,
                    kind="wordpress",
                    encrypted_credentials="fixture-only",
                    status="connected",
                    capabilities={
                        "seo": {
                            "write": True,
                            "writable_fields": ["title", "description"],
                        }
                    },
                )
            )
            source = initial_source(site.id, site.origin)
            states[site.id] = source
            page = workflows.store_page(db, site, source)
            workflows.upsert_observation(
                db,
                site,
                page,
                {
                    "signals": {},
                    "findings": [],
                    "candidates": [
                        {
                            "field": "seo_title",
                            "before_value": source["title"],
                            "after_value": f"Complete fixture title for {site.id}",
                        },
                        {
                            "field": "meta_description",
                            "before_value": source["metadata"]["seo"]["forgeseo"]["description"],
                            "after_value": "Complete fixture description with useful repair guidance for visitors.",
                        },
                    ],
                },
            )
        db.commit()

        candidates_a = list(
            db.scalars(
                select(Candidate)
                .where(Candidate.site_id == site_a.id)
                .order_by(Candidate.field)
            )
        )
        candidates_b = list(
            db.scalars(
                select(Candidate)
                .where(Candidate.site_id == site_b.id)
            )
        )
        assert {candidate.field for candidate in candidates_a} == {"meta_description", "seo_title"}
        assert len(candidates_b) == 2
        selected = next(candidate for candidate in candidates_a if candidate.field == "seo_title")
        sibling = next(candidate for candidate in candidates_a if candidate.field == "meta_description")
        foreign = candidates_b[0]

        approved = decide(
            site_a.id,
            selected.id,
            Decision(decision="approve"),
            {"user_id": "fixture-owner", "team_id": site_a.team_id, "role": "owner"},
            db,
        )
        assert approved["status"] == "approved"

        with pytest.raises(HTTPException) as wrong_site:
            candidate_for_site(db, selected.id, site_b.id)
        assert wrong_site.value.status_code == 404
        db.rollback()

        with pytest.raises(ValueError, match="does not belong to this site"):
            asyncio.run(
                workflows.candidate(
                    db,
                    site_b,
                    Job(
                        id="foreign-candidate-job",
                        site_id=site_b.id,
                        kind="candidate",
                        payload={"candidate_id": selected.id},
                    ),
                )
            )
        db.rollback()

        result = asyncio.run(
            workflows.candidate(
                db,
                site_a,
                Job(
                    id="selected-candidate-job",
                    site_id=site_a.id,
                    kind="candidate",
                    payload={"candidate_id": selected.id},
                ),
            )
        )
        db.commit()
        assert result["status"] == "applied"

        replay = asyncio.run(
            workflows.candidate(
                db,
                site_a,
                Job(
                    id="selected-candidate-replay",
                    site_id=site_a.id,
                    kind="candidate",
                    payload={"candidate_id": selected.id},
                ),
            )
        )
        assert replay == {"status": "applied", "candidate_id": selected.id, "replayed": True}
        db.commit()

        db.expire_all()
        assert db.get(Candidate, selected.id).status == "applied"
        assert db.get(Candidate, sibling.id).status == "pending"
        assert db.get(Candidate, sibling.id).source_hash == states[site_a.id]["source_hash"]
        assert db.get(Candidate, foreign.id).status == "pending"
        assert db.get(Candidate, foreign.id).source_hash == f"source-{site_b.id}"

        with pytest.raises(HTTPException) as repeated_selection:
            decide(
                site_a.id,
                selected.id,
                Decision(decision="approve"),
                {"user_id": "fixture-owner", "team_id": site_a.team_id, "role": "owner"},
                db,
            )
        assert repeated_selection.value.status_code == 409


def test_postgres_concurrent_observations_preserve_one_active_candidate(
    postgres_database: PostgresDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two workers observing one page cannot create duplicate active work."""

    from app import workflows

    site_id = _site(postgres_database.factory, paused=False)
    observation = {
        "signals": {},
        "findings": [],
        "candidates": [
            {
                "field": "seo_title",
                "before_value": "Old title",
                "after_value": "A complete repair article title",
            }
        ],
    }

    with postgres_database.factory() as first_db:
        first_site = first_db.get(Site, site_id)
        assert first_site is not None
        page = Page(
            site_id=site_id,
            resource_key="posts:concurrent-observation",
            resource_type="posts",
            url="https://concurrent.fixture.invalid/article",
            source_hash="concurrent-source",
        )
        first_db.add(page)
        first_db.commit()
        page_id = page.id

        # Hold the row lock before starting the second worker. The worker must
        # remain blocked until the first transaction commits; without the
        # workflow lock it would insert a duplicate candidate immediately.
        workflows._lock_observation_page(first_db, first_site, page)
        second_started = Event()
        original_lock = workflows._lock_observation_page

        def wrapped_lock(db, site, observed_page):
            second_started.set()
            return original_lock(db, site, observed_page)

        monkeypatch.setattr(workflows, "_lock_observation_page", wrapped_lock)

        def observe_in_second_worker() -> None:
            with postgres_database.factory() as second_db:
                second_site = second_db.get(Site, site_id)
                second_page = second_db.get(Page, page_id)
                assert second_site is not None and second_page is not None
                workflows.upsert_observation(second_db, second_site, second_page, observation)
                second_db.commit()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(observe_in_second_worker)
            assert second_started.wait(timeout=5)
            assert not future.done()

            workflows.upsert_observation(first_db, first_site, page, observation)
            first_db.commit()
            future.result(timeout=5)

    with postgres_database.factory() as db:
        candidates = db.scalars(
            select(Candidate).where(
                Candidate.site_id == site_id,
                Candidate.page_id == page_id,
                Candidate.status.in_(
                    ["pending", "approved", "applied", "rejected", "failed"]
                ),
            )
        ).all()
        assert len(candidates) == 1
        assert candidates[0].after_value == "A complete repair article title"


def test_postgres_encrypted_backup_restore_preserves_artifacts_and_safety(
    postgres_database: PostgresDatabase,
    postgres_live: PostgresFixture,
    tmp_path: Path,
) -> None:
    from app.connectors.security import decrypt_credentials, encrypt_credentials
    from scripts.backup import create_backup, restore_backup

    credential_key = Fernet.generate_key().decode("ascii")
    backup_key = Fernet.generate_key()
    artifact_body = b"<p>isolated PostgreSQL evidence</p>\n"
    source_artifacts = tmp_path / "source-artifacts"

    with postgres_database.factory() as db:
        team = Team(name="Backup integration team")
        user = User(
            email="backup-owner@fixture.invalid",
            name="Backup owner",
            password_hash="test-only-password-hash",
        )
        db.add_all([team, user])
        db.flush()
        db.add(Membership(team_id=team.id, user_id=user.id, role="owner"))
        active_site = Site(
            team_id=team.id,
            name="Restored active site",
            origin="https://active.postgres.fixture.invalid",
            paused=False,
            facts={"business_name": "Active Fixture", "services": ["repairs"]},
        )
        paused_site = Site(
            team_id=team.id,
            name="Restored paused site",
            origin="https://paused.postgres.fixture.invalid",
            paused=True,
            facts={"business_name": "Paused Fixture", "services": ["repairs"]},
        )
        db.add_all([active_site, paused_site])
        db.flush()
        db.add(
            Connection(
                site_id=active_site.id,
                kind="wordpress",
                encrypted_credentials=encrypt_credentials(
                    {"username": "fixture-user", "password": "fixture-only-secret"},
                    credential_key,
                ),
                status="connected",
            )
        )
        db.add(
            AuthSession(
                user_id=user.id,
                token_hash=hashlib.sha256(b"fixture-session-token").hexdigest(),
                csrf_token=hashlib.sha256(b"fixture-csrf-token").hexdigest(),
                expires_at=utcnow() + timedelta(days=1),
            )
        )
        db.add_all(
            [
                Job(
                    site_id=active_site.id,
                    kind="publish",
                    status="running",
                    payload={"fixture": "interrupted-write"},
                    result={},
                    idempotency_key="backup-interrupted-running",
                    attempts=1,
                    lease_until=utcnow() - timedelta(minutes=5),
                ),
                Job(
                    site_id=paused_site.id,
                    kind="audit",
                    status="queued",
                    payload={"fixture": "interrupted-read"},
                    result={},
                    idempotency_key="backup-interrupted-queued",
                    attempts=0,
                ),
                Heartbeat(
                    name="platform_controls",
                    last_seen_at=utcnow(),
                    details={"global_pause": False},
                ),
            ]
        )
        db.commit()
        artifact_name = f"evidence/{active_site.id}/fixture.html"

    artifact_path = source_artifacts / Path(artifact_name)
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(artifact_body)

    payload = create_backup(
        postgres_database.engine,
        source_artifacts,
        backup_key,
        credential_key,
    )
    assert b"fixture-only-secret" not in payload

    with _temporary_database(postgres_live) as destination:
        with pytest.raises(InvalidToken):
            restore_backup(
                destination.engine,
                tmp_path / "wrong-backup-key",
                payload,
                Fernet.generate_key(),
                credential_key,
            )
        with pytest.raises(ValueError, match="Original credential key"):
            restore_backup(
                destination.engine,
                tmp_path / "wrong-credential-key",
                payload,
                backup_key,
                Fernet.generate_key().decode("ascii"),
            )
        with pytest.raises(ValueError, match="Artifact checksum mismatch"):
            restore_backup(
                destination.engine,
                tmp_path / "tampered-artifact",
                _tamper_artifact(payload, backup_key, artifact_name),
                backup_key,
                credential_key,
            )

        restored_artifacts = tmp_path / "restored-artifacts"
        result = restore_backup(
            destination.engine,
            restored_artifacts,
            payload,
            backup_key,
            credential_key,
        )
        assert result == {
            # Backup intentionally omits the sessions table so a restored
            # deployment cannot revive browser credentials.
            "tables": len(Base.metadata.sorted_tables) - 1,
            "artifacts": 1,
            "automation": "paused",
            "browser_sessions": "invalidated",
        }
        assert (restored_artifacts / Path(artifact_name)).read_bytes() == artifact_body
        assert hashlib.sha256(
            (restored_artifacts / Path(artifact_name)).read_bytes()
        ).hexdigest() == hashlib.sha256(artifact_body).hexdigest()

        with destination.factory() as db:
            sites = {
                site.name: site.paused
                for site in db.scalars(select(Site)).all()
            }
            assert sites == {
                "Restored active site": True,
                "Restored paused site": True,
            }
            assert db.scalar(select(Connection.encrypted_credentials)) is not None
            encrypted = db.scalar(select(Connection.encrypted_credentials))
            assert decrypt_credentials(encrypted, credential_key) == {
                "username": "fixture-user",
                "password": "fixture-only-secret",
            }
            assert db.scalar(select(AuthSession.id)) is None
            jobs = {
                job.idempotency_key: job.status
                for job in db.scalars(select(Job)).all()
            }
            assert jobs == {
                "backup-interrupted-running": "needs_reconciliation",
                "backup-interrupted-queued": "needs_reconciliation",
            }
