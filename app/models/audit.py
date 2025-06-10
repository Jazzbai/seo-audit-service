from pydantic import BaseModel, HttpUrl

class AuditRequest(BaseModel):
    """
    Pydantic model for the audit request body.
    Ensures that the provided URL is a valid HTTP/HTTPS URL.
    """
    url: HttpUrl
    max_pages: int = 100
    max_depth: int = 5

class AuditResponse(BaseModel):
    """
    Pydantic model for the initial audit response.
    """
    task_id: str
    message: str 