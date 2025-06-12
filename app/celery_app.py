# IMPORTANT: This import is no longer needed.
# import app.utils.serialization

from celery import Celery
from app.core.config import settings

# Create the Celery application instance
# The `include` argument is a list of modules to import when the worker starts.
# This is the most reliable way to ensure our tasks are discovered.
celery_app = Celery(
    "seo_audit_agent",
    include=['app.tasks.orchestrator', 'app.tasks.seo_tasks']
)

# Load configuration directly from our Pydantic settings object.
celery_app.conf.update(settings.model_dump())

@celery_app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}') 