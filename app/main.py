from fastapi import FastAPI
from celery.result import AsyncResult
from workers.celery_app import app as celery_app
from workers.tasks import add_together

app = FastAPI(title="SEO Audit Agent API", description="API for triggering and managing SEO audits.", version="1.0.0")

@app.get("/", tags=["Health Check"])
async def root():
    """
    A simple health check endpoint.
    """
    return {"message": "API is running."}

@app.post("/test-task", status_code=202, tags=["Tasks"])
async def run_test_task():
    """
    Triggers the simple 'add_together' test task.
    
    This endpoint sends the task to the Celery worker via the message broker
    and immediately returns a response containing the ID of the task.
    """
    task = add_together.delay(5, 5)
    return {"task_id": task.id}

@app.get("/results/{task_id}", tags=["Tasks"])
async def get_task_result(task_id: str):
    """
    Retrieves the result of a specific task.
    
    You can call this endpoint after getting a task_id from `/test-task`.
    It will check the task's status and, if completed, return the result.
    """
    task_result = AsyncResult(task_id, app=celery_app)
    
    if task_result.ready():
        # The task has completed.
        if task_result.successful():
            # The task completed successfully.
            return {
                "task_id": task_id,
                "status": "SUCCESS",
                "result": task_result.get(),
            }
        else:
            # The task failed.
            return {
                "task_id": task_id,
                "status": "FAILURE",
                "error": str(task_result.info), # Contains the exception information.
            }
    else:
        # The task is still pending or running.
        return {"task_id": task_id, "status": "PENDING"} 