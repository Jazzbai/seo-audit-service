from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import settings

api_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)


async def get_api_key(api_key: str = Security(api_key_header)):
    """
    Dependency to validate the API key from the X-API-KEY header.

    Compares the provided API key with the one loaded from the environment
    settings. If it's missing or incorrect, it raises an HTTP 401 Unauthorized
    error.
    """
    if not api_key or api_key != settings.API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API Key",
        )
    return api_key
