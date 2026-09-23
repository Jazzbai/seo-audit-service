from cryptography.fernet import Fernet

from scripts.preflight import validate_environment


def production_environment():
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


def test_preflight_accepts_production_environment_without_printing_or_contacting_services():
    assert validate_environment(production_environment()) == []
    assert validate_environment(production_environment(), backup=True) == []


def test_preflight_reports_all_missing_and_insecure_production_settings():
    errors = validate_environment({})
    assert "DB_PASSWORD is missing" in errors
    assert "ENCRYPTION_KEY is missing" in errors
    assert "PUBLIC_URL is missing" in errors
    assert "COOKIE_SECURE must be true in production" in errors
    assert "APP_ADDRESS is missing" in errors


def test_preflight_rejects_placeholders_http_and_missing_backup_key():
    environment = production_environment()
    environment.update(
        {
            "DB_PASSWORD": "change-me-" + "x" * 30,
            "PUBLIC_URL": "http://localhost:18080",
            "APP_ADDRESS": "localhost",
            "COOKIE_SECURE": "false",
            "BACKUP_KEY": "",
        }
    )
    errors = validate_environment(environment, backup=True)
    assert "DB_PASSWORD still contains a placeholder value" in errors
    assert "PUBLIC_URL must use https in production" in errors
    assert "APP_ADDRESS must be the deployed public hostname" in errors
    assert "COOKIE_SECURE must be true in production" in errors
    assert "BACKUP_KEY is missing" in errors


def test_preflight_rejects_non_fernet_backup_key_but_accepts_ordinary_encryption_key():
    environment = production_environment()
    environment["BACKUP_KEY"] = "backup-" + "x" * 40

    assert validate_environment(environment, backup=True) == [
        "BACKUP_KEY must be a valid Fernet key"
    ]


def test_preflight_rejects_reused_application_secrets():
    environment = production_environment()
    environment["QUEUE_PASSWORD"] = environment["DB_PASSWORD"]

    errors = validate_environment(environment)

    assert "QUEUE_PASSWORD must be different from DB_PASSWORD" in errors


def test_preflight_rejects_backup_key_reused_as_an_application_secret():
    environment = production_environment()
    environment["ENCRYPTION_KEY"] = environment["BACKUP_KEY"]

    errors = validate_environment(environment, backup=True)

    assert "BACKUP_KEY must be different from ENCRYPTION_KEY" in errors


def test_preflight_rejects_a_public_url_with_a_path():
    environment = production_environment()
    environment["PUBLIC_URL"] = "https://seo.forgeseo.com/app"

    errors = validate_environment(environment)

    assert errors == ["PUBLIC_URL must be an origin without a path"]


def test_preflight_accepts_a_root_public_url_path():
    environment = production_environment()
    environment["PUBLIC_URL"] = "https://seo.forgeseo.com/"

    assert validate_environment(environment) == []


def test_preflight_rejects_a_one_sided_backup_mirror_configuration():
    for name in ("BACKUP_MIRROR_DIRECTORY", "BACKUP_MIRROR_HOST_PATH"):
        environment = production_environment()
        environment[name] = "/configured/backup-mirror"

        errors = validate_environment(environment, backup=True)

        assert errors == [
            "BACKUP_MIRROR_DIRECTORY and BACKUP_MIRROR_HOST_PATH must be set together"
        ]
        assert "/configured/backup-mirror" not in " ".join(errors)
