import eventlet
eventlet.monkey_patch()

from fastapi import FastAPI
from celery.result import AsyncResult
from pydantic import BaseModel

# Import our Celery app instance and the task
from workers.celery_app import app as celery_app
from workers.tasks import add_together
from workers.orchestrator import run_full_audit
from app.models.audit import AuditRequest, AuditResponse

# Define a Pydantic model for our task response
class TaskResponse(BaseModel):
    task_id: str
    status: str

app = FastAPI(
    title="SEO Audit Agent API", 
    description="API for triggering and managing SEO audits.", 
    version="1.0.0"
)

@app.get("/", tags=["Health Check"])
async def root():
    """A simple health check endpoint to confirm the API is running."""
    return {"message": "API is running."}

@app.post("/v1/audits", response_model=AuditResponse, status_code=202, tags=["Audit"])
async def create_audit(request: AuditRequest):
    """
    Starts a new SEO audit for the given URL.
    
    This triggers the master orchestrator task and returns a task ID
    which can be used to check the status and retrieve the results.
    """
    # Pydantic v2 returns a special URL object, we need to convert it to a string for Celery
    url_str = str(request.url)
    task = run_full_audit.delay(url=url_str)
    return {"task_id": task.id, "message": "Audit successfully started."}

@app.post("/test-task", status_code=202, response_model=TaskResponse, tags=["Tasks"])
async def run_test_task():
    """
    Triggers the simple 'add_together' test task.
    
    This sends the task to the Celery worker and immediately returns
    the task ID for tracking.
    """
    task = add_together.delay(5, 5)
    return {"task_id": task.id, "status": "PENDING"}

@app.get("/results/{task_id}", tags=["Tasks"])
async def get_task_result(task_id: str):
    """
    Retrieves the result of a specific task by its ID.
    
    Call this endpoint after getting a task_id from `/test-task`.
    It checks the task's status and, if completed, returns the result.
    """
    task_result = AsyncResult(task_id, app=celery_app)
    
    if not task_result.ready():
        return {"task_id": task_id, "status": "PENDING"}

    if task_result.successful():
        return {
            "task_id": task_id,
            "status": "SUCCESS",
            "result": task_result.get(),
        }
    else:
        # The task failed. Return the error information.
        return {
            "task_id": task_id,
            "status": "FAILURE",
            "error": str(task_result.info),
        } 