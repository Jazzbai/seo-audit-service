import time
import random
from .celery_app import app

# A placeholder for exceptions that might occur due to network issues, etc.
class TransientException(Exception):
    pass

@app.task(
    bind=True, 
    autoretry_for=(TransientException,), 
    retry_kwargs={'max_retries': 3, 'countdown': 5}
)
def add_together(self, a: int, b: int) -> int:
    """
    A simple test task that adds two numbers.
    - `bind=True` gives us access to `self` (the task instance).
    - `autoretry_for` tells Celery to automatically retry this task if it
      raises a TransientException.
    - `retry_kwargs` specifies that it should retry a maximum of 3 times,
      waiting 5 seconds between retries.
    """
    print(f"Executing task {self.request.id}, attempt {self.request.retries + 1}")

    # Simulate a transient failure that happens occasionally
    if self.request.retries < 2 and random.choice([True, False]):
        print("Simulating a transient failure...")
        raise TransientException("Could not connect to a temporary service")

    result = a + b
    print(f"Task {self.request.id} completed successfully with result: {result}")
    return result 