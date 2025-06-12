from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    """
    Manages application settings using Pydantic.
    
    This class automatically reads environment variables or values from a .env file
    and validates them against the defined types.
    """
    # Use aliases to map uppercase .env variables to lowercase attributes
    broker_url: str = Field(..., alias='BROKER_URL')
    result_backend: str = Field(..., alias='RESULT_BACKEND')
    DATABASE_URL: str # This is already uppercase, so no alias needed

    # Lowercase Celery settings for modern configuration
    task_serializer: str = 'json'
    result_serializer: str = 'json'
    accept_content: list[str] = ['json']

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