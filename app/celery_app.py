from celery import Celery
from app.core.config import settings

# Create the Celery application instance
celery_app = Celery("seo_audit_agent")

# Load Celery configuration from our Pydantic settings object.
# Celery will automatically look for uppercase attributes starting with 'CELERY_'.
celery_app.config_from_object(settings, namespace='CELERY')

# Set the result backend explicitly from the loaded settings
celery_app.conf.result_backend = settings.CELERY_RESULT_BACKEND

# Configure Celery to automatically discover tasks.
# It will look for a 'tasks' module in each of the specified packages.
celery_app.autodiscover_tasks(['app.tasks'])

@celery_app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}') 