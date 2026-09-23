"""Isolated browser-to-WordPress acceptance server; never a deployment entrypoint.

Uses a disposable SQLite database and an in-process durable-job consumer. Real
WordPress/WooCommerce HTTP is routed ONLY to the two existing Docker fixtures.
No paid provider, real site, existing environment file, or production database
is used. RabbitMQ/PostgreSQL and browser rendering have separate test gates.
"""
import os
import queue
import secrets
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def main():
    os.chdir(ROOT)
    # Reuse the explicit fixture's allowlisted transport and setup. Capture
    # credentials in memory only; never print them or write browser traces.
    with tempfile.TemporaryDirectory(prefix='forge-pilot-browser-') as temp:
        os.environ.update(
            DATABASE_URL='sqlite:///' + (Path(temp) / 'pilot.db').as_posix(),
            ENCRYPTION_KEY=secrets.token_urlsafe(48), BROKER_URL='memory://',
            GLOBAL_PAUSE='true', COOKIE_SECURE='false',
            PUBLIC_URL='http://127.0.0.1:4173',
            BOOTSTRAP_TOKEN='test-only-bootstrap-token',
            ARTIFACT_ROOT=str(Path(temp) / 'artifacts'),
        )
        from test_wordpress_live import configure, FixtureTransport, integration_stack
        from app import workflows, worker, network
        from app.connectors.wordpress import WordPressClient
        from app.connectors.woocommerce import WooCommerceClient
        from app.intelligence import audit
        from app.operations import credentials
        from app.main import app
        from app.db import engine
        import uvicorn

        stack = integration_stack.__wrapped__()
        next(stack)
        try:
            fixtures = {'wordpress': configure(), 'woocommerce': configure('woo', 'woo')}

            async def fixture_client(db, site, kind='wordpress'):
                secret, _ = credentials(db, site.id, kind)
                connector = WooCommerceClient if kind == 'woocommerce' else WordPressClient
                return connector(site.origin, secret, transport=FixtureTransport())

            async def fixture_fetch(url):
                return await network.fetch(url, transport=FixtureTransport())

            workflows.client_for = fixture_client
            workflows.fetch = fixture_fetch
            audit._default_transport = lambda origin=None: FixtureTransport()
            jobs = queue.Queue()
            stop = object()

            def dispatch(args, **kwargs):
                # Browser rendering is deliberately not simulated as successful.
                # Its jobs remain queued in this source-HTML acceptance fixture.
                if kwargs.get('queue') != 'browser':
                    jobs.put(args[0])

            worker.execute_job.apply_async = dispatch

            def consume():
                while True:
                    job_id = jobs.get()
                    if job_id is stop:
                        return
                    worker.run_job(job_id)

            consumer = threading.Thread(target=consume, daemon=True)
            consumer.start()

            @app.get('/__fixture', include_in_schema=False)
            def fixture_settings():
                # Only this loopback-bound test server installs this endpoint.
                return fixtures

            try:
                uvicorn.run(app, host='127.0.0.1', port=18082, log_level='warning')
            finally:
                jobs.put(stop)
                consumer.join(timeout=30)
                engine.dispose()
        finally:
            stack.close()


if __name__ == '__main__':
    main()
