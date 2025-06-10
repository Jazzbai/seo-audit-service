import time
from .celery_app import app


@app.task(bind=True)
def add_together(self, a: int, b: int) -> int:
    """
    A simple test task that adds two numbers.
    - `bind=True` gives us access to `self` (the task instance),
      which is useful for tracking and custom states.
    - We add a time.sleep() to simulate a real-world task that isn't instant.
    """
    print(f"Executing task {self.request.id}...")
    time.sleep(5)
    result = a + b
    print(f"Task {self.request.id} completed with result: {result}")
    return result 