import os
from celery import Celery
from dotenv import load_dotenv

# It's a good practice to load environment variables at the start.
# This ensures that os.getenv can access the values from the .env file.
load_dotenv()

# Retrieve the broker URL from environment variables.
# The `os.getenv` function is the standard way to do this,
# providing a secure way to configure the app without hardcoding credentials.
celery_broker_url = os.getenv("CELERY_BROKER_URL")

# The result backend is set to 'rpc://'. This is a special configuration
# that tells Celery to send results back to the client that initiated the task
# through the same AMQP (RabbitMQ) connection. This is highly efficient and
# avoids the need for another service like Redis.
celery_result_backend = "rpc://"


# Create the Celery application instance.
# The first argument 'workers' is the name of the current module.
# This is important for Celery's autodiscovery of tasks.
app = Celery(
    "workers",
    broker=celery_broker_url,
    backend=celery_result_backend,
    include=["workers.tasks"],  # List of modules to import when the worker starts.
)

# Optional configuration for Celery.
# For example, you can set the timezone here.
app.conf.update(
    task_track_started=True,
) 