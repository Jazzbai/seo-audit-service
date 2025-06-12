from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """
    Manages application settings using Pydantic.
    
    This class automatically reads environment variables or values from a .env file
    and validates them against the defined types.
    """
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    class Config:
        # This tells Pydantic to look for a .env file if the environment variables are not set.
        env_file = ".env"
        env_file_encoding = "utf-8"

# Create a single, importable instance of the settings.
# Throughout the application, we will import this 'settings' object.
settings = Settings() 