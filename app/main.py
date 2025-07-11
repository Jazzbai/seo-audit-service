import datetime
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.orm import Session

from app.api.dependencies import get_api_key
from app.db.session import Base, engine, get_db
from app.models.audit import Audit
from app.tasks.orchestrator import run_full_audit

# Load environment variables from .env file
load_dotenv()

# Create all database tables on startup (for development)
# In production, you would rely solely on Alembic migrations.
Base.metadata.create_all(bind=engine)

# --- Pydantic Models ---


class AuditRequest(BaseModel):
    """The request model for starting a new audit."""

    url: HttpUrl
    max_pages: Optional[int] = Field(
        100, description="The maximum number of pages to crawl."
    )
    user_id: Optional[str] = None
    user_audit_report_request_id: Optional[str] = None


class AuditResponse(BaseModel):
    """The response model for a successfully launched audit."""

    audit_id: int
    task_id: str
    status: str


class AuditResultResponse(BaseModel):
    """The response model for a retrieved audit result."""

    audit_id: int = Field(validation_alias="id")
    status: str
    url: str
    user_id: Optional[str] = None
    user_audit_report_request_id: Optional[str] = None
    created_at: datetime.datetime
    completed_at: Optional[datetime.datetime]
    error_message: Optional[str] = None
    technical_error: Optional[str] = None
    report_json: Optional[Any] = None

    class Config:
        from_attributes = True


# --- FastAPI Application ---

app = FastAPI(
    title="SEO Audit Agent API",
    description="API for triggering and managing SEO audits.",
    version="0.4.0",  # Version for Postgres integration
)

# --- API Endpoints ---


@app.get("/", tags=["Health Check"])
async def root():
    """A simple health check endpoint to confirm the API is running."""
    return {"message": "API is running and ready to accept tasks."}


@app.post("/v1/audits", response_model=AuditResponse, status_code=202, tags=["Audits"])
async def start_new_audit(
    request: AuditRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(get_api_key),
):
    """
    Triggers the full SEO audit workflow.

    This endpoint creates a new record in the database, then launches the
    Celery orchestrator task to perform the crawl and analysis.
    """
    url_str = str(request.url)

    # 1. Create a record in our database
    new_audit = Audit(
        url=url_str,
        status="PENDING",
        user_id=request.user_id,
        user_audit_report_request_id=request.user_audit_report_request_id,
    )
    db.add(new_audit)
    db.commit()
    db.refresh(new_audit)

    # 2. Launch the background task with the new audit ID and max_pages
    task = run_full_audit.delay(
        audit_id=new_audit.id, url=url_str, max_pages=request.max_pages
    )

    return {"audit_id": new_audit.id, "task_id": task.id, "status": "PENDING"}


@app.get("/v1/audits/{audit_id}", response_model=AuditResultResponse, tags=["Audits"])
async def get_audit_result(
    audit_id: int, db: Session = Depends(get_db), api_key: str = Depends(get_api_key)
):
    """
    Retrieves the status and results of an audit from the database.

    This provides the full report for a completed audit, or the current
    status for an audit in progress.
    """
    audit_record = db.query(Audit).filter(Audit.id == audit_id).first()
    if not audit_record:
        raise HTTPException(status_code=404, detail="Audit not found")

    # By using a response_model with from_attributes=True, we can return
    # the SQLAlchemy model directly. Pydantic will handle mapping the
    # `audit_record.report_json` to the `report` field in the response model.
    return audit_record
