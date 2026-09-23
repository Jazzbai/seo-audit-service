from cryptography.fernet import Fernet

from scripts.preflight import validate_environment


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


def test_backup_preflight_rejects_retention_values_the_cleanup_command_cannot_use():
    environment = _production_environment()
    environment["BACKUP_RETENTION_DAYS"] = "not-a-number"

    errors = validate_environment(environment, backup=True)

    assert errors == ["BACKUP_RETENTION_DAYS must be a positive whole number"]
    assert environment["BACKUP_KEY"] not in " ".join(errors)
