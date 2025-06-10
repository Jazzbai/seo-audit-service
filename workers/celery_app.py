import eventlet
eventlet.monkey_patch()

from celery import Celery
from app.core.config import settings

# This is the core Celery application instance.
# All configuration is now sourced from the centralized Pydantic settings object,
# ensuring that settings are validated at startup.
app = Celery(
    "workers",
    broker=settings.CELERY_BROKER_URL,
    backend="rpc://",
    include=["workers.tasks", "workers.seo_tasks", "workers.orchestrator"]
)

app.conf.update(
    task_track_started=True,
) 