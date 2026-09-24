"""Isolated integration-test subprocess; never a production worker entrypoint.

Fixture credentials arrive on stdin, not command-line arguments or logs.
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def main():
    payload = json.load(sys.stdin)
    fixture = payload['fixture']
    if fixture.get('origin') != 'https://wordpress.fixture.test':
        raise ValueError('Only the isolated WordPress fixture is permitted')
    database = Path(payload['database']).resolve(strict=True)
    if database.name != 'recovery-fixture.db':
        raise ValueError('Unexpected integration database')

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import worker, workflows
    from app.config import settings
    from app.connectors.wordpress import WordPressClient
    from app.network import fetch
    from test_wordpress_live import FixtureTransport

    engine = create_engine('sqlite:///' + database.as_posix())
    worker.engine = engine
    worker.SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    settings.GLOBAL_PAUSE = False
    settings.ARTIFACT_ROOT = str(database.parent / 'recovery-artifacts')

    class CrashAfterRemoteCreate(WordPressClient):
        async def create_draft(self, article, operation_key):
            record = await super().create_draft(article, operation_key)
            if payload['crash_after_create']:
                # Real process termination: no exception handler, local remote-ID
                # commit, context-manager cleanup or in-memory state survives.
                os._exit(73)
            return record

    async def client_for(db, site, kind='wordpress'):
        if site.origin != fixture['origin'] or kind != 'wordpress':
            raise ValueError('Request escaped the isolated WordPress fixture')
        return CrashAfterRemoteCreate(site.origin, fixture, transport=FixtureTransport())

    async def public_fetch(url):
        return await fetch(url, transport=FixtureTransport())

    workflows.client_for = client_for
    workflows.fetch = public_fetch
    result = worker.run_job(payload['job_id'])
    print(json.dumps({'status': result.get('status'), 'complete': result.get('complete'),
                      'reason': result.get('reason')}))
    engine.dispose()


if __name__ == '__main__':
    main()
