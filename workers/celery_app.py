import eventlet
eventlet.monkey_patch()

import os
from celery import Celery
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

celery_broker_url = os.getenv("CELERY_BROKER_URL")

# This is the core Celery application instance.
# 'workers' is the name of the module where tasks will be defined.
# The broker URL is read from our environment variables for security and flexibility.
# The backend is set to 'rpc://', which reuses the broker connection for results.
# This is efficient and avoids needing a separate service like Redis.
app = Celery(
    "workers",
    broker=celery_broker_url,
    backend="rpc://",
    include=["workers.tasks"]  # Automatically discover tasks in this module.
)

app.conf.update(
    task_track_started=True,
) 