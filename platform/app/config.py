"""Application configuration for the standalone ForgeSEO pilot.

Configuration is deliberately environment-driven. The defaults are suitable
for a local development checkout and do not contain credentials or other
secrets.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by the foundation and application layers."""

    DATABASE_URL: str = "sqlite:///./data/forgeseo.db"
    BROKER_URL: str = "amqp://localhost:5672//"
    ENCRYPTION_KEY: str = ""
    COOKIE_SECURE: bool = False
    GLOBAL_PAUSE: bool = True
    PUBLIC_URL: str = "http://localhost"
    # Required by the deployment for the one-time first-owner bootstrap.
    # An empty local default fails closed; tests and disposable local servers
    # must set an explicit test-only value.
    BOOTSTRAP_TOKEN: str = ""
    ARTIFACT_ROOT: str = "./artifacts"

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )


settings = Settings()
