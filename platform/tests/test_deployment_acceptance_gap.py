from cryptography.fernet import Fernet
from pathlib import Path

from scripts.preflight import validate_environment


ROOT = Path(__file__).resolve().parents[1]


def _production_environment():
    return {
        "DB_PASSWORD": "db-" + "x" * 40,
        "QUEUE_PASSWORD": "queue-" + "x" * 40,
        "ENCRYPTION_KEY": "encrypt-" + "x" * 40,
        "BOOTSTRAP_TOKEN": "bootstrap-" + "x" * 40,
        "PUBLIC_URL": "https://seo.forgeseo.com",
        "APP_ADDRESS": "seo.forgeseo.com",
        "COOKIE_SECURE": "true",
        "BACKUP_KEY": Fernet.generate_key().decode("ascii"),
    }


def test_backup_preflight_rejects_a_freshness_window_shorter_than_two_runs():
    environment = _production_environment()
    environment.update(
        {
            "BACKUP_INTERVAL_SECONDS": "86400",
            "BACKUP_MAX_AGE_SECONDS": "3600",
        }
    )

    errors = validate_environment(environment, backup=True)

    assert errors == [
        "BACKUP_MAX_AGE_SECONDS must be at least twice BACKUP_INTERVAL_SECONDS"
    ]


def test_backup_preflight_rejects_non_positive_interval_without_exposing_values():
    environment = _production_environment()
    environment["BACKUP_INTERVAL_SECONDS"] = "0"

    errors = validate_environment(environment, backup=True)

    assert errors == ["BACKUP_INTERVAL_SECONDS must be a positive number"]
    assert environment["BACKUP_KEY"] not in " ".join(errors)


def test_caddy_security_headers_include_a_long_lived_hsts_policy():
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert 'Strict-Transport-Security "max-age=31536000"' in caddyfile
