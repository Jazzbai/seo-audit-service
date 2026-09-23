"""Disposable local API for real-browser integration tests. Never uses existing env credentials."""
import os
import secrets
import sys
import tempfile
from pathlib import Path


def main():
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root))
    os.chdir(root)
    with tempfile.TemporaryDirectory(prefix='forgeseo-browser-test-') as temp:
        os.environ.update(DATABASE_URL='sqlite:///'+str(Path(temp)/'test.db').replace('\\','/'),
            ENCRYPTION_KEY=secrets.token_urlsafe(48),BROKER_URL='memory://',GLOBAL_PAUSE='true',COOKIE_SECURE='false',
            PUBLIC_URL='http://127.0.0.1:4173',BOOTSTRAP_TOKEN='test-only-bootstrap-token',
            ARTIFACT_ROOT=str(Path(temp)/'artifacts'))
        import uvicorn
        uvicorn.run('app.main:app',host='127.0.0.1',port=18082,log_level='warning')
        from app.db import engine
        engine.dispose()


if __name__=='__main__':
    main()
