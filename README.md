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

## Setup and Running

This project is designed to be run with Docker Compose, which simplifies setup. However, you can also run the services locally for development.

### 1. Recommended Setup with Docker Compose

This is the easiest and recommended way to get started. It runs all services (FastAPI, Celery, Postgres, RabbitMQ) in isolated containers.

**Prerequisites:**
- **Docker** and **Docker Compose**

**Steps:**

1.  **Clone the Repository**
    ```bash
    git clone <repository-url>
    cd seo-audit
    ```

2.  **Create and Configure the `.env` File**

    Copy the example file to `.env`. This file holds all your configuration.
    ```bash
    cp .env.example .env
    ```
    Open the new `.env` file and **set a secure `API_KEY`**. You can also change the `API_PORT` if the default `8001` conflicts with another service.

3.  **Build and Run the Containers**
    ```bash
    docker-compose up --build
    ```
    This single command builds the Docker images and starts all services. Database migrations are run automatically by the `web` service on startup.

    The API will be available at `http://127.0.0.1:8001` (or your custom port).

### 2. Manual Local Setup (Advanced)

This approach is for developers who want to run the Python services directly on their host machine while using Docker for backing services like Postgres and RabbitMQ.

**Prerequisites:**
- **Python 3.12**
- **PostgreSQL**: A running instance is required. See database setup notes below.
- **RabbitMQ**: A running instance is required.
- **Docker** and **Docker Compose** (can be used to run Postgres and RabbitMQ easily)

**Steps:**

1.  **Prepare Backing Services**

    You can run PostgreSQL and RabbitMQ natively on your machine, or you can use Docker to easily run them.

    *Using Docker (Recommended for simplicity):*
    ```bash
    docker-compose up -d postgres-db rabbitmq
    ```
    *Manual Installation:* Ensure both services are installed and running on their default ports.

2.  **Create the Database and User (Manual PostgreSQL Setup)**
    
    If you are running PostgreSQL locally (not in Docker), you will need to manually create the database and user required by the application.
    
    Connect to PostgreSQL using the `psql` command-line tool:
    ```bash
    psql -U postgres
    ```
    
    Then, run the following SQL commands one by one. These commands create the user and database as defined in the `.env.example` file. **Remember to use the same password you plan to set in your `.env` file.**

    ```sql
    -- Create a dedicated user for the application
    CREATE USER seo_audit_user WITH PASSWORD 'your_strong_password';

    -- Create the database
    CREATE DATABASE seo_audit_db;

    -- Grant all privileges on the new database to the new user
    GRANT ALL PRIVILEGES ON DATABASE seo_audit_db TO seo_audit_user;

    -- Exit the psql client
    \q
    ```

3.  **Create and Activate Virtual Environment**

    It's crucial to use a virtual environment to manage dependencies.

    *On Windows (using PowerShell):*
    ```powershell
    # Create the venv using Python 3.12
    py -3.12 -m venv venv

    # Activate it
    .\venv\Scripts\Activate.ps1
    ```

    *On macOS / Linux:*
    ```bash
    # Create the venv using Python 3.12
    python3.12 -m venv venv

    # Activate it
    source venv/bin/activate
    ```
    Your terminal prompt should now be prefixed with `(venv)`.

4.  **Install Dependencies**
    ```bash
    pip install -r requirements.txt
    ```

5.  **Configure the `.env` File**

    Create the `.env` file if you haven't already (`cp .env.example .env`). **Crucially, you must edit it** to point to your local services.
    - Change `postgres-db` and `rabbitmq` to `localhost`.
    - Set your `API_KEY` and desired `API_PORT`.

6.  **Run Database Migrations**

    Apply any pending database migrations.
    ```bash
    alembic upgrade head
    ```

7.  **Run the Application**

    You need to run two processes in separate terminals (both with the `venv` activated).

    - **Terminal 1: Run FastAPI Server**
      ```bash
      # The port will be controlled by the API_PORT in your .env file
      uvicorn app.main:app --reload --port 8001
      ```

    - **Terminal 2: Run Celery Worker**
      ```bash
      # Use the thread pool for better performance on Windows for I/O tasks
      celery -A app.celery_app.celery_app worker -P threads --concurrency=10 --loglevel=info
      ```

## Using the API

You can interact with the API using tools like `curl`, Postman, or the auto-generated interactive documentation. The API is protected, so you must include your `API_KEY`.

**Interactive Docs**: `http://127.0.0.1:8001/docs` (or your custom port)

### 1. Start a New Audit

Send a `POST` request to `/v1/audits`.

**Example Request:**

```bash
curl -X POST "http://127.0.0.1:8001/v1/audits" \
-H "Content-Type: application/json" \
-H "X-API-KEY: your_secret_api_key_here" \
-d '{
  "url": "https://textennis.com/",
  "max_pages": 20,
  "user_id": "user-123",
  "user_audit_report_request_id": "request-abc-789"
}'
```

### 2. Retrieve the Audit Report

Use the `audit_id` from the previous step to fetch the full report.

**Example Request:**

```bash
curl -X GET "http://127.0.0.1:8001/v1/audits/138" \
-H "X-API-KEY: your_secret_api_key_here"
```

**Example Response (`200 OK`):**

The response will contain the full, detailed report.

```json
{
    "audit_id": 138,
    "status": "COMPLETE",
    "url": "http://textennis.com/",
    "user_id": "user-123",
    "user_audit_report_request_id": "request-abc-789",
    "created_at": "2025-06-21T23:07:07.922573",
    "completed_at": "2025-06-21T23:07:43.734928",
    "report_json": {
        "status": "COMPLETE",
        "audit_id": 138,
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

The project includes a utility script, `view_result.py`, to fetch the raw result of a specific Celery task directly from the result backend. This is useful for debugging individual task failures.

**Usage:**

- **Local Development:** Run the script directly with your virtual environment activated.
  ```bash
  python view_result.py <celery_task_id>
  ```

- **Docker:** Run the script inside the `fastapi-app` container.
  ```bash
  docker-compose exec fastapi-app python view_result.py <celery_task_id>
  ``` 