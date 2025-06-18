# SEO Audit Agent

A production-grade, asynchronous SEO audit agent built with FastAPI and Celery. It leverages the `advertools` library to perform robust web crawls and generate detailed on-page and external link reports.

The agent is containerized with Docker for easy setup and deployment, providing a simple REST API to trigger audits and retrieve detailed, page-by-page reports in JSON format.

## Key Features

- **Asynchronous Workflow**: Uses Celery to run audits in the background without blocking the API.
- **Robust & Polite Crawling**: Employs `advertools` for efficient website crawling, with configurable politeness settings (e.g., download delay).
- **Detailed On-Page SEO Analysis**: Checks for:
    - Page Title presence and keyword analysis.
    - Meta Description presence.
    - H1 Heading rules (ensuring exactly one per page).
- **Comprehensive Link Checking**:
    - **Internal Links**: Identifies all broken internal links (4xx/5xx errors).
    - **External Links**: Intelligently categorizes external link issues to provide actionable insights:
        - **Unreachable**: Links that could not be reached due to network errors (e.g., DNS failure).
        - **Broken**: Links that returned a definitive `404 Not Found` or `410 Gone`.
        - **Permission Issues**: Links that returned a `403 Forbidden`, indicating they are likely blocking crawlers.
        - **Method Issues**: Links that returned a `405 Method Not Allowed`, indicating a technical incompatibility with the checker.
- **RESTful API**: A simple FastAPI interface for starting audits and fetching results.
- **Persistent Storage**: Uses PostgreSQL to store audit status and the final JSON reports.
- **Containerized**: Fully configured with Docker and `docker-compose` for a one-command setup.

## Project Architecture

The agent is composed of several key components that run in separate Docker containers:

1.  **FastAPI Application (`fastapi-app`)**: Provides the API endpoints. It handles incoming requests, creates audit jobs in the database, and dispatches them to the Celery worker via the RabbitMQ broker.
2.  **Celery Worker (`celery-worker`)**: The background process that picks up tasks from the queue and executes the audit workflow (crawling, analysis, etc.).
3.  **RabbitMQ (`rabbitmq`)**: The message broker that facilitates communication between the FastAPI app and the Celery workers.
4.  **PostgreSQL (`postgres-db`)**: The main application database. It stores the overall status of each audit job and the final, compiled JSON report via the `Audit` SQLAlchemy model. It also serves as the Celery Result Backend.
5.  **Alembic**: Handles database schema migrations, ensuring the database schema is up-to-date with the models.

## Setup and Installation

### Prerequisites

- **Docker** and **Docker Compose**
- **Python 3.12** (for local development and `pip freeze`)

### 1. Clone the Repository

```bash
git clone <repository-url>
cd seo-audit
```

### 2. Create the Environment File

This project uses a `.env` file to manage environment variables. Create a file named `.env` in the project root by copying the example.

```bash
# In a real project, you would have a .env.example file.
# For now, create .env and add the following content:
```

```dotenv
# .env file

# 1. Broker URL for RabbitMQ
# This is the default for the RabbitMQ container in docker-compose.
BROKER_URL="amqp://guest:guest@rabbitmq:5672//"

# 2. Celery Result Backend URL
# This uses the PostgreSQL container.
# Format: postgresql+psycopg://<user>:<password>@<host>:<port>/<db_name>
RESULT_BACKEND="postgresql+psycopg://seo_audit_user:your_strong_password@postgres-db:5432/seo_audit_db"

# 3. Main Application Database URL (for SQLAlchemy)
DATABASE_URL="postgresql+psycopg://seo_audit_user:your_strong_password@postgres-db:5432/seo_audit_db"
```

**Note:** The `DATABASE_URL` and `RESULT_BACKEND` use the service name `postgres-db` as the host, which is how Docker Compose networking resolves the container's IP address.

### 3. Freeze Dependencies (Best Practice)

For a stable and reproducible environment, it's best to "freeze" the exact versions of all Python packages.

```bash
# First, ensure you are in the correct virtual environment
python -m venv venv-py312
source venv-py312/bin/activate  # On macOS/Linux
# venv-py312\Scripts\activate  # On Windows

# Install dependencies if you haven't
pip install -r requirements.txt

# Now, freeze the exact versions
pip freeze > requirements.txt
```

## How to Run the Agent

With Docker and Docker Compose, running the entire application stack is a single command.

```bash
docker-compose up --build
```

This command will:
1.  Build the `fastapi-app` and `celery-worker` Docker images based on the `Dockerfile`.
2.  Start containers for the FastAPI app, Celery worker, PostgreSQL, and RabbitMQ.
3.  The API will be available at `http://127.0.0.1:8000`.

### Running Database Migrations

After starting the application for the first time, you may need to run the database migrations to create the necessary tables. Open a new terminal and run:

```bash
docker-compose exec fastapi-app alembic upgrade head
```

## Using the API

You can interact with the API using tools like `curl`, Postman, or the auto-generated interactive documentation.

**Interactive Docs**: `http://127.0.0.1:8000/docs`

### 1. Start a New Audit

Send a `POST` request to the `/v1/audits` endpoint with the URL you want to analyze.

**Example Request:**

```bash
curl -X POST "http://127.0.0.1:8000/v1/audits" \
-H "Content-Type: application/json" \
-d '{
  "url": "https://textennis.com/privacy-policy/",
  "max_pages": 20
}'
```

**Example Response (`202 Accepted`):**

This response confirms the audit has been accepted and provides the ID to track it.

```json
{
  "audit_id": 119,
  "task_id": "36c8bb14-546d-48ba-ad19-5030005fcf06",
  "status": "PENDING"
}
```

### 2. Retrieve the Audit Report

Use the `audit_id` from the previous step to fetch the full report with a `GET` request.

**Example Request:**

```bash
curl -X GET "http://127.0.0.1:8000/v1/audits/119"
```

**Example Response (`200 OK`):**

The response will contain the full, detailed report with our new categorized link analysis.

```json
{
    "id": 119,
    "status": "COMPLETE",
    "url": "https://textennis.com/privacy-policy/",
    "created_at": "2025-06-18T17:30:38.196042",
    "completed_at": "2025-06-18T17:31:30.018334",
    "report_json": {
        "status": "COMPLETE",
        "audit_id": 119,
        "summary": {
            "total_pages_analyzed": 20,
            "internal_broken_links_found": 0,
            "external_unreachable_links_found": 1,
            "external_broken_links_found": 1,
            "external_permission_issues_found": 26,
            "external_method_issues_found": 2,
            "external_other_client_errors_found": 0,
            "pages_missing_title": 0,
            "pages_missing_meta_description": 20,
            "pages_with_correct_h1": 10,
            "pages_with_multiple_h1s": 8,
            "pages_with_no_h1": 2
        },
        "external_unreachable_links": [
            {
                "url": "https://www.eugdpr.org/",
                "status": "Unreachable",
                "source_url": "https://textennis.com/privacy-policy/"
            }
        ],
        "external_broken_links": [
            {
                "url": "https://www.youtube.com/static?template=privacy_guidelines",
                "status": 404,
                "source_url": "https://textennis.com/privacy-policy/"
            }
        ],
        "external_permission_issue_links": [
            {
                "url": "https://twitter.com/ThemerexThemes",
                "status": 403,
                "source_url": "https://textennis.com/privacy-policy/"
            }
        ],
        "external_method_issue_links": [
            {
                "url": "https://www.amazon.com/",
                "status": 405,
                "source_url": "https://textennis.com/shop/"
            }
        ],
        "internal_broken_links": [],
        "page_level_report": {
            "https://textennis.com/privacy-policy/": [
                {
                    "status": "SUCCESS",
                    "check": "title",
                    "value": "Privacy Policy - Tennis Club",
                    "message": "Title found."
                }
            ]
        }
    }
}
```

## Debugging Utilities

### Viewing Raw Celery Task Results

The project includes a utility script, `view_result.py`, to fetch the raw result of a specific Celery task directly from the result backend. This is useful for debugging individual task failures if they don't complete successfully.

**Usage:**

Run this command from within the running `fastapi-app` container:

```bash
docker-compose exec fastapi-app python view_result.py <celery_task_id>
``` 