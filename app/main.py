from fastapi import FastAPI, Depends, HTTPException
from pydantic import BaseModel, HttpUrl
from celery.result import AsyncResult
from dotenv import load_dotenv
from sqlalchemy.orm import Session

# Import our new Celery app instance and the async task
from app.celery_app import celery_app
from app.tasks.orchestrator import run_full_audit
from app.db.session import get_db, Base, engine
from app.models.audit import Audit

# Load environment variables from .env file
load_dotenv()

# Create all database tables on startup (for development)
# In production, you would rely solely on Alembic migrations.
Base.metadata.create_all(bind=engine)

# --- Pydantic Models ---

class AuditRequest(BaseModel):
    """The request model for starting a new audit."""
    url: HttpUrl

class AuditResponse(BaseModel):
    """The response model for a successfully launched audit."""
    audit_id: int
    task_id: str
    status: str

# --- FastAPI Application ---

app = FastAPI(
    title="SEO Audit Agent API",
    description="API for triggering and managing SEO audits.",
    version="0.4.0" # Version for Postgres integration
)

# --- API Endpoints ---

@app.get("/", tags=["Health Check"])
async def root():
    """A simple health check endpoint to confirm the API is running."""
    return {"message": "API is running and ready to accept tasks."}

@app.post("/v1/audits", response_model=AuditResponse, status_code=202, tags=["Audits"])
async def start_new_audit(request: AuditRequest, db: Session = Depends(get_db)):
    """
    Triggers the full SEO audit workflow.
    
    This endpoint creates a new record in the database, then launches the
    Celery orchestrator task to perform the crawl and analysis.
    """
    url_str = str(request.url)

    # 1. Create a record in our database
    new_audit = Audit(url=url_str, status="PENDING")
    db.add(new_audit)
    db.commit()
    db.refresh(new_audit)

    # 2. Launch the background task with the new audit ID
    task = run_full_audit.delay(audit_id=new_audit.id, url=url_str)
    
    return {
        "audit_id": new_audit.id,
        "task_id": task.id,
        "status": "PENDING"
    }

@app.get("/v1/audits/{audit_id}", tags=["Audits"])
async def get_audit_status(audit_id: int, db: Session = Depends(get_db)):
    """
    Retrieves the status and results of an audit from the database.
    """
    audit = db.query(Audit).filter(Audit.id == audit_id).first()
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found")
        
    return {
        "audit_id": audit.id,
        "status": audit.status,
        "url": audit.url,
        "created_at": audit.created_at,
        "completed_at": audit.completed_at,
        "report": audit.report_json
    } 