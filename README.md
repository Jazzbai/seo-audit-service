# SEO Audit Agent

This project is a production-grade, asynchronous SEO audit agent built with FastAPI, Celery, and PostgreSQL. It leverages the `advertools` library to perform robust and polite web crawls, analyzing on-page SEO factors for an entire website.

The agent is designed to be resilient, handling server errors and rate-limiting gracefully through a multi-layered retry mechanism. It provides a simple REST API to trigger audits and retrieve detailed, page-by-page reports in JSON format.

## Key Features

- **Asynchronous Workflow**: Uses Celery to run audits in the background without blocking the API.
- **Robust Crawling**: Employs `advertools` for efficient and polite website crawling, respecting `robots.txt` and controlling request rates.
- **Resilient Task Execution**: Features a two-layer retry system:
    1.  **HTTP-Level Retries**: Automatically retries on transient 5xx server errors.
    2.  **Task-Level Retries**: Uses Celery's exponential backoff to handle `429 Too Many Requests` errors from servers with aggressive rate-limiting.
- **Detailed SEO Analysis**: Checks for:
    - Page Title presence and content.
    - Meta Description presence and content.
    - H1 Heading rules (exactly one per page).
    - Broken links (4xx and 5xx status codes) on a per-page basis.
- **RESTful API**: A simple FastAPI interface for starting audits and fetching results.
- **Persistent Storage**: Uses PostgreSQL to store audit status and the final JSON reports.

## Project Architecture

The agent is composed of several key components:

1.  **FastAPI Application (`app/main.py`)**: Provides the API endpoints to the outside world. It handles incoming requests, creates audit jobs in the database, and dispatches them to the Celery worker.
2.  **Celery (`app/celery_app.py`)**: The distributed task queue. It manages the background execution of the audit workflow, ensuring that long-running tasks don't tie up the API server.
3.  **RabbitMQ (Broker)**: The message broker that facilitates communication between the FastAPI app and the Celery workers. The app sends "start audit" messages here.
4.  **Celery Worker**: The process that picks up tasks from the RabbitMQ queue and executes them. This is where the crawling and analysis actually happen.
5.  **PostgreSQL (Result Backend & Database)**:
    - As a **Result Backend**, it stores the state and final results of individual Celery tasks.
    - As the main **application database**, it stores the overall status of each audit job and the final, compiled JSON report via the `Audit` SQLAlchemy model.
6.  **Advertools**: The core crawling engine. It replaces a custom-built crawler to provide professional-grade, polite, and configurable web crawling capabilities.

## Setup and Installation

### Prerequisites

- Python 3.10+
- PostgreSQL Server
- RabbitMQ Server

### 1. Clone the Repository

```bash
git clone <repository-url>
cd seo-audit-agent
```

### 2. Set Up Environment

This project uses a `.env` file to manage environment variables. Create a file named `.env` in the project root and add the following variables, replacing the values with your local setup details:

```dotenv
# .env file

# URL for your RabbitMQ broker
BROKER_URL="amqp://guest:guest@localhost:5672//"

# URL for your PostgreSQL database (Celery Result Backend)
# Format: postgresql+psycopg://<user>:<password>@<host>:<port>/<db_name>
RESULT_BACKEND="postgresql+psycopg://seo_audit_user:password@localhost:5432/seo_audit_db"

# URL for the main application database (SQLAlchemy)
DATABASE_URL="postgresql+psycopg://seo_audit_user:password@localhost:5432/seo_audit_db"
```

**Note:** You will need to create a user and a database in PostgreSQL that matches these credentials.

### 3. Install Dependencies

It is highly recommended to use a virtual environment.

```bash
# Create a virtual environment
python -m venv venv

# Activate it
# On Windows
venv\Scripts\activate
# On macOS/Linux
source venv/bin/activate

# Install the required packages
pip install -r requirements.txt
```

### 4. Database Migrations

This project uses Alembic for database migrations. Although the application will create tables on startup for development convenience, it's best practice to use migrations.

*This step is for completeness; for initial setup, the automatic table creation is sufficient.*

## How to Run the Agent

You need to run three separate processes in three separate terminals.

### Terminal 1: Run the FastAPI Server

This server provides the HTTP API.

```bash
uvicorn app.main:app --reload
```
The API will be available at `http://127.0.0.1:8000`. You can view the interactive documentation at `http://127.0.0.1:8000/docs`.

### Terminal 2: Run the Celery Worker

This is the background worker that will execute the audit tasks. The `-P threads` flag is essential for running on Windows.

```bash
celery -A app.celery_app worker -P threads --loglevel=info
```

### Terminal 3: Keep for API Requests

Use this terminal to interact with the API using a tool like `curl` or Postman.

## Using the API

### 1. Start a New Audit

Send a `POST` request to the `/v1/audits` endpoint with the URL you want to analyze.

**Example Request:**

```bash
curl -X POST "http://127.0.0.1:8000/v1/audits" \
-H "Content-Type: application/json" \
-d '{
  "url": "http://leobeautycenter.com/"
}'
```

**Example Response (`202 Accepted`):**

```json
{
  "audit_id": 1,
  "task_id": "ab8e2c1f-7b0c-4e8a-9a4f-3c1e2b8d0f6a",
  "status": "PENDING"
}
```

### 2. Retrieve the Audit Report

Once the audit is complete, use the `audit_id` from the previous step to fetch the full report with a `GET` request.

**Example Request:**

```bash
curl -X GET "http://127.0.0.1:8000/v1/audits/1"
```

**Example Response (`200 OK`):**

The response will contain the full, detailed report, structured by URL.

```json
{
    "audit_id": 1,
    "status": "COMPLETE",
    "url": "http://leobeautycenter.com/",
    "created_at": "2025-06-12T22:40:18.289611",
    "completed_at": "2025-06-12T22:42:56.976652",
    "report": {
        "status": "COMPLETE",
        "audit_id": 1,
        "total_pages_analyzed": 79,
        "report": {
            "https://leobeautycenter.com/": [
                {
                    "status": "SUCCESS",
                    "check": "title",
                    "value": "Leo Spa – Beauty Center in Sugarland, TX",
                    "message": "Title found."
                },
                {
                    "status": "FAILURE",
                    "check": "meta_description",
                    "value": null,
                    "message": "Meta description not found or is empty."
                },
                {
                    "status": "FAILURE",
                    "check": "broken_links",
                    "value": [
                        {
                            "url": "https://leobeautycenter.com/about-features/",
                            "status_code": 404
                        }
                    ],
                    "message": "Found 1 broken links on this page."
                }
            ],
            "https://leobeautycenter.com/about/": [
                // ... results for this page
            ]
        }
    }
}
```

## Debugging Utilities

### Viewing Raw Task Results

The project includes a utility script, `view_result.py`, to fetch the raw result of a specific Celery task directly from the result backend. This is useful for debugging individual task failures.

**Usage:**

```bash
python view_result.py <celery_task_id>
``` 