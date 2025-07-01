import json
import sys

from celery.result import AsyncResult

from app.celery_app import celery_app


def view_task_result(task_id: str):
    """
    Connects to the Celery result backend and retrieves the
    result for a given task ID, printing it in a readable format.
    """
    if not task_id:
        print("Error: Please provide a task ID.")
        print("Usage: python view_result.py <task_id>")
        return

    print(f"Fetching result for task: {task_id}")

    # Create an AsyncResult object for the given task ID.
    # This object knows how to talk to the configured result backend.
    result = AsyncResult(task_id, app=celery_app)

    if result.ready():
        if result.successful():
            print("\n--- Task Succeeded ---")
            task_result = result.get()
            print("Result:")
            # Pretty-print the JSON result
            print(json.dumps(task_result, indent=4))
        else:
            print("\n--- Task Failed ---")
            # result.get() will re-raise the exception
            try:
                result.get()
            except Exception as e:
                print(f"Exception: {e!r}")
                print(f"\nTraceback:\n{result.traceback}")
    else:
        print("\n--- Task Not Ready ---")
        print(f"Current state: {result.state}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        task_id_to_view = sys.argv[1]
        view_task_result(task_id_to_view)
    else:
        print("Usage: python view_result.py <task_id>")
