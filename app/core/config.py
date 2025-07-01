from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Manages application settings using Pydantic.

    This class automatically reads environment variables or values from a .env file
    and validates them against the defined types.
    """

    # Use aliases to map uppercase .env variables to lowercase attributes
    broker_url: str = Field(..., alias="BROKER_URL")
    result_backend: str = Field(..., alias="RESULT_BACKEND")
    DATABASE_URL: str  # This is already uppercase, so no alias needed

    # API Security
    API_KEY: str = Field(..., alias="API_KEY")

    # API Server Port
    API_PORT: int = Field(8000, alias="API_PORT")

    # Dashboard Callback Settings (Optional)
    DASHBOARD_CALLBACK_URL: Optional[str] = Field(None, alias="DASHBOARD_CALLBACK_URL")
    DASHBOARD_API_KEY: Optional[str] = Field(None, alias="DASHBOARD_API_KEY")

    # Celery Queue Configuration (prevents interference with other workers)
    CELERY_QUEUE_NAME: str = Field("seo_audit_queue", alias="CELERY_QUEUE_NAME")

    # Lowercase Celery settings for modern configuration
    task_serializer: str = "json"
    result_serializer: str = "json"
    accept_content: list[str] = ["json"]

    # Queue configuration - use our dedicated queue
    task_default_queue: str = "seo_audit_queue"

    # Task routing - ensures all our tasks go to our queue
    task_routes: dict = {
        "app.tasks.orchestrator.*": {"queue": "seo_audit_queue"},
        "app.celery_app.*": {"queue": "seo_audit_queue"},
    }

    class Config:
        # This tells Pydantic to look for a .env file if the environment variables are not set.
        env_file = ".env"
        env_file_encoding = "utf-8"
        # This allows Pydantic to read an environment variable by its alias
        # (e.g., read BROKER_URL) and populate the `broker_url` attribute.
        populate_by_name = True


# Create a single, importable instance of the settings.
# Throughout the application, we will import this 'settings' object.
settings = Settings()
