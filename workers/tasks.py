import time
from .celery_app import app


@app.task
def add_together(a: int, b: int) -> int:
    """
    A simple test task that adds two numbers together.
    We add a time.sleep() to simulate a task that takes some time to run,
    which is a more realistic scenario for our future SEO tasks.
    """
    time.sleep(5)
    return a + b 