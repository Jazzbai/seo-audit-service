from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_env_example_documents_the_encrypted_backup_timing_contract():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "BACKUP_KEY=" in example
    assert "BACKUP_INTERVAL_SECONDS=86400" in example
    assert "BACKUP_RETENTION_DAYS=14" in example
    assert "BACKUP_MAX_AGE_SECONDS=172800" in example
    assert "BACKUP_MIRROR_DIRECTORY=" in example
    assert "BACKUP_MIRROR_HOST_PATH=" in example
