"""Isolated browser-to-WordPress acceptance server; never a deployment entrypoint.

Uses a disposable SQLite database and an in-process durable-job consumer. Real
WordPress/WooCommerce HTTP is routed ONLY to the two existing Docker fixtures.
No paid provider, real site, existing environment file, or production database
is used. RabbitMQ/PostgreSQL have separate test gates. Optional Chromium
inspection uses the production handler with only fixture HTTP routing replaced.
"""
import os
import argparse
import json
import queue
import secrets
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path
from contextlib import nullcontext

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wordpress-only', action='store_true')
    parser.add_argument('--scheduled-publication', action='store_true')
    parser.add_argument('--render-pages', action='store_true', help='Run the real Chromium handler against allowlisted fixtures')
    parser.add_argument('--retain-evidence', action='store_true')
    parser.add_argument('--resume-evidence', help='Existing retained WordPress-only rehearsal directory')
    args = parser.parse_args()
    os.chdir(ROOT)
    # Reuse the explicit fixture's allowlisted transport and setup. Capture
    # credentials in memory only; never print them or write browser traces.
    if args.resume_evidence:
        retained_root = (ROOT / 'artifacts' / 'publishing-rehearsals').resolve()
        directory = Path(args.resume_evidence).resolve()
        if not args.wordpress_only or retained_root not in directory.parents or not (directory / 'pilot.db').is_file():
            raise SystemExit('Only an existing retained WordPress-only rehearsal can resume')
        with sqlite3.connect(f'file:{(directory / "pilot.db").as_posix()}?mode=ro', uri=True) as previous:
            origins = [row[0] for row in previous.execute('SELECT origin FROM sites')]
        if origins != ['https://wordpress.fixture.test']:
            raise SystemExit('Resume requires exactly the isolated WordPress fixture; no other site is allowed')
        args.retain_evidence = True
        storage = nullcontext(str(directory))
    elif args.retain_evidence:
        retained_root = ROOT / 'artifacts' / 'publishing-rehearsals'
        retained_root.mkdir(parents=True, exist_ok=True)
        directory = tempfile.mkdtemp(prefix='run-', dir=retained_root)
        storage = nullcontext(directory)
    else:
        storage = tempfile.TemporaryDirectory(prefix='forge-pilot-browser-')
    with storage as temp:
        # Local fixture key only. Retain it with the ignored fixture database so
        # interrupted test processes can resume; this is NOT production custody.
        key_file = Path(temp) / 'fixture-encryption.key'
        key_reused = key_file.is_file()
        key = key_file.read_text(encoding='utf-8') if key_reused else secrets.token_urlsafe(48)
        if args.retain_evidence and not key_reused:
            with key_file.open('x', encoding='utf-8') as handle:
                handle.write(key)
            key_file.chmod(0o600)
        os.environ.update(
            DATABASE_URL='sqlite:///' + (Path(temp) / 'pilot.db').as_posix(),
            ENCRYPTION_KEY=key, BROKER_URL='memory://',
            GLOBAL_PAUSE='true', COOKIE_SECURE='false',
            PUBLIC_URL='http://127.0.0.1:4173',
            BOOTSTRAP_TOKEN='test-only-bootstrap-token',
            ARTIFACT_ROOT=str(Path(temp) / 'artifacts'),
        )
        from test_wordpress_live import configure, FixtureTransport, integration_stack
        from app import workflows, worker, network, scheduler, browser
        from app.connectors.wordpress import WordPressClient
        from app.connectors.woocommerce import WooCommerceClient
        from app.intelligence import audit
        from app.operations import credentials
        from app.main import app
        from app.db import engine, SessionLocal
        from app.models import Article, Publication, Job, Site
        from sqlalchemy import select
        import uvicorn

        # The WordPress-only rehearsal must reuse a running fixture and must
        # not install, reconfigure or start WooCommerce as a side effect.
        stack = None
        if not args.wordpress_only:
            stack = integration_stack.__wrapped__()
            next(stack)
        try:
            fixtures = {'wordpress': configure()}
            if not args.wordpress_only:
                fixtures['woocommerce'] = configure('woo', 'woo')

            async def fixture_client(db, site, kind='wordpress'):
                secret, _ = credentials(db, site.id, kind)
                connector = WooCommerceClient if kind == 'woocommerce' else WordPressClient
                return connector(site.origin, secret, transport=FixtureTransport())

            async def fixture_fetch(url):
                return await network.fetch(url, transport=FixtureTransport())

            workflows.client_for = fixture_client
            workflows.fetch = fixture_fetch
            audit._default_transport = lambda origin=None: FixtureTransport()
            if args.render_pages:
                def fixture_browser_transport(origin):
                    if origin not in {item['origin'] for item in fixtures.values()}:
                        raise ValueError('Browser escaped the isolated fixture allowlist')
                    return FixtureTransport()
                browser.PublicTransport = fixture_browser_transport
            jobs = queue.Queue()
            stop = object()
            stop_schedule = threading.Event()
            schedule_errors = []
            run_browser_jobs = args.render_pages

            def dispatch(args, **kwargs):
                # Either run real Chromium with the fixture transport or retain
                # queued renderer jobs; never simulate successful rendering.
                if kwargs.get('queue') != 'browser' or run_browser_jobs:
                    jobs.put(args[0])

            worker.execute_job.apply_async = dispatch

            def scheduled_publications():
                while not stop_schedule.wait(2):
                    try:
                        # Do not race bootstrap or create unrelated scheduled
                        # work before the UI has explicitly scheduled an article.
                        with SessionLocal() as db:
                            ready = db.scalar(select(Article.id).where(Article.status == 'scheduled'))
                        if ready:
                            scheduler.schedule()
                    except Exception as exc:
                        # Never echo credentials or exception text. Do not
                        # simulate a successful tick after a real failure.
                        schedule_errors.append(type(exc).__name__)

            def consume():
                while True:
                    job_id = jobs.get()
                    if job_id is stop:
                        return
                    worker.run_job(job_id)

            consumer = threading.Thread(target=consume, daemon=True)
            consumer.start()
            if args.resume_evidence and run_browser_jobs:
                # Restore only the explicit rehearsal's read-only render queue.
                # Never redispatch publishing or unrelated historical audits.
                with SessionLocal() as db:
                    for audit_job in db.scalars(select(Job).where(Job.kind == 'audit')):
                        if not (audit_job.idempotency_key or '').startswith(f'{audit_job.site_id}:rehearsal-render:'):
                            continue
                        for job_id in (audit_job.result or {}).get('browser_job_ids', []):
                            render_job = db.get(Job, job_id)
                            if (render_job and render_job.kind == 'browser'
                                    and render_job.site_id == audit_job.site_id
                                    and render_job.status == 'queued'):
                                jobs.put(job_id)
            ticker = None
            if args.scheduled_publication:
                ticker = threading.Thread(target=scheduled_publications, daemon=True)
                ticker.start()

            @app.get('/__fixture', include_in_schema=False)
            def fixture_settings():
                # Only this loopback-bound test server installs this endpoint.
                return fixtures

            @app.get('/__rehearsal', include_in_schema=False)
            def rehearsal_evidence():
                # No keys, fixture passwords, cookie values or provider calls.
                with SessionLocal() as db:
                    sites = [{'id': s.id, 'origin': s.origin} for s in db.scalars(select(Site))]
                    articles = [{'id': a.id, 'status': a.status, 'remote_id': a.remote_id} for a in db.scalars(select(Article).where(Article.remote_id.is_not(None)))]
                return {'evidence_directory': str(temp), 'wordpress_only': args.wordpress_only,
                        'resumed': bool(args.resume_evidence), 'fixture_key_reused': key_reused,
                        'sites': sites, 'articles_with_remote': articles,
                        'scheduled_publication': args.scheduled_publication,
                        'real_chromium_handler': args.render_pages,
                        'scheduler_errors': schedule_errors,
                        'transport': 'real loopback WordPress HTTP; explicit fixture routing',
                        'queue': 'in-process job delivery; production worker.run_job'}

            try:
                uvicorn.run(app, host='127.0.0.1', port=18082, log_level='warning')
            finally:
                stop_schedule.set()
                if ticker:
                    ticker.join(timeout=10)
                jobs.put(stop)
                consumer.join(timeout=30)
                if args.retain_evidence:
                    with SessionLocal() as db:
                        report = {
                            'scope': 'isolated WordPress UI rehearsal, not seven-day acceptance',
                            'scheduler_errors': schedule_errors,
                            'sites': [{'id': s.id, 'origin': s.origin, 'paused': s.paused} for s in db.scalars(select(Site))],
                            'articles': [{'id': a.id, 'status': a.status, 'remote_id': a.remote_id} for a in db.scalars(select(Article))],
                            'publications': [{'id': p.id, 'article_id': p.article_id, 'status': p.status, 'remote_id': p.remote_id} for p in db.scalars(select(Publication))],
                            'jobs': [{'id': j.id, 'kind': j.kind, 'status': j.status} for j in db.scalars(select(Job))],
                        }
                    (Path(temp) / 'rehearsal-summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
                engine.dispose()
        finally:
            if stack is not None:
                stack.close()


if __name__ == '__main__':
    main()
