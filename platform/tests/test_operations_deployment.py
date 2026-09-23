import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from deploy.healthcheck import worker_replies_healthy
from scripts.preflight import validate_environment


ROOT=Path(__file__).resolve().parents[1]


def _production_environment():
    return {
        'DB_PASSWORD': 'db-' + 'x' * 40,
        'QUEUE_PASSWORD': 'queue-' + 'x' * 40,
        'ENCRYPTION_KEY': 'encrypt-' + 'x' * 40,
        'BOOTSTRAP_TOKEN': 'bootstrap-' + 'x' * 40,
        'PUBLIC_URL': 'https://seo.forgeseo.com',
        'COOKIE_SECURE': 'true',
        'APP_ADDRESS': 'seo.forgeseo.com',
    }


def test_preflight_requires_proxy_address_to_match_public_url_host():
    environment = _production_environment()
    assert validate_environment(environment) == []

    environment['APP_ADDRESS'] = 'other.forgeseo.com'

    errors = validate_environment(environment)

    assert 'APP_ADDRESS must match the PUBLIC_URL hostname' in errors


def test_worker_health_probe_requires_expected_worker_family():
    assert worker_replies_healthy({'platform@worker-1': {'ok': 'pong'}}, 'platform')
    assert not worker_replies_healthy({'scheduler@worker-1': {'ok': 'pong'}}, 'platform')
    assert not worker_replies_healthy({'platform@worker-1': {'ok': 'error'}}, 'platform')
    assert not worker_replies_healthy({}, 'platform')


@pytest.mark.skipif(shutil.which('docker') is None, reason='Docker CLI is not installed')
def test_production_compose_renders_runtime_health_and_backup_profile():
    environment=os.environ.copy()
    environment.update({
        'DB_PASSWORD': 'operations-compose-db',
        'QUEUE_PASSWORD': 'operations-compose-queue',
        'ENCRYPTION_KEY': 'x'*44,
        'BOOTSTRAP_TOKEN': 'test-only-bootstrap-token',
        'PUBLIC_URL': 'https://seo.example.test',
        'COOKIE_SECURE': 'true',
        'APP_ADDRESS': 'seo.example.test',
        'BACKUP_KEY': 'y'*44,
        'BACKUP_MIRROR_DIRECTORY': '',
        'BACKUP_MIRROR_HOST_PATH': '',
    })
    result=subprocess.run(
        ['docker','compose','--profile','backup','config','--format','json'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode==0, result.stderr
    model=json.loads(result.stdout)
    services=model['services']
    for name in ('api','worker','scheduler-worker','beat','browser','web','backup'):
        assert 'healthcheck' in services[name], name
    assert services['backup']['profiles']==['backup']
    assert services['backup-init']['profiles']==['backup', 'restore']
    assert 'service_completed_successfully' in str(services['backup']['depends_on'])
    assert 'service_completed_successfully' in str(services['api']['depends_on'])
    assert services['backup']['environment']['BACKUP_MIRROR_DIRECTORY'] == ''
    assert any(volume['target']=='/srv/backup-mirror' for volume in services['backup']['volumes'])

    mirror_environment=environment.copy()
    mirror_environment.update({
        'BACKUP_MIRROR_DIRECTORY': '/srv/backup-mirror',
        'BACKUP_MIRROR_HOST_PATH': 'D:/secure-forgeseo-backups',
    })
    mirror_result=subprocess.run(
        ['docker','compose','--profile','backup','config','--format','json'],
        cwd=ROOT,
        env=mirror_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert mirror_result.returncode==0, mirror_result.stderr
    mirror_model=json.loads(mirror_result.stdout)
    mirror_service=mirror_model['services']['backup']
    assert mirror_service['environment']['BACKUP_MIRROR_DIRECTORY']=='/srv/backup-mirror'
    assert any(
        volume['source']=='D:/secure-forgeseo-backups'
        and volume['target']=='/srv/backup-mirror'
        for volume in mirror_service['volumes']
    )

    restore_result=subprocess.run(
        ['docker','compose','--profile','restore','config','--format','json'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert restore_result.returncode==0, restore_result.stderr
    restore_services=json.loads(restore_result.stdout)['services']
    assert restore_services['restore']['profiles']==['restore']
    assert restore_services['restore']['depends_on']['backup-init']['condition']=='service_completed_successfully'
    assert any(volume['source']=='artifacts' and volume['target']=='/srv/artifacts' for volume in restore_services['restore']['volumes'])
    assert any(volume['source']=='backups' and volume['target']=='/srv/backups' for volume in restore_services['restore']['volumes'])
