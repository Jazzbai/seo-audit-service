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

This project can be run locally for development or as a fully containerized application using Docker Compose for production. The recommended approach for development and testing is the local setup.

### 1. Local Development Setup

This is the most direct way to run the agent for development and testing.

**Prerequisites:**
- **Python 3.12**
- **PostgreSQL**: Must be installed and running.
- **RabbitMQ**: Must be installed and running.
- **Alembic**: For database migrations.

*Learning Note: For convenience, you can still use Docker to easily run PostgreSQL and RabbitMQ locally without installing them on your system. You can use the `postgres-db` and `rabbitmq` services from the `docker-compose.yml` file and just run the FastAPI application on your host machine.*

**Steps:**

1.  **Clone the Repository**
    ```bash
    git clone <repository-url>
    cd seo-audit
    ```

2.  **Create and Activate Virtual Environment**
    ```bash
    python -m venv venv
    source venv/bin/activate      # On macOS/Linux
    # venv\Scripts\activate       # On Windows
    ```

3.  **Install Dependencies**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Create the `.env` File**

    Create a file named `.env` in the project root. For local development, the host for your database and broker will be `localhost`.

    ```dotenv
    # .env file for Local Development

    # 1. Broker URL for RabbitMQ
    BROKER_URL="amqp://guest:guest@localhost:5672//"

    # 2. Celery Result Backend (PostgreSQL)
    RESULT_BACKEND="postgresql+psycopg://your_db_user:your_db_password@localhost:5432/seo_audit_db"

    # 3. Main Application Database (SQLAlchemy)
    DATABASE_URL="postgresql+psycopg://your_db_user:your_db_password@localhost:5432/seo_audit_db"
    ```
    *Note: Remember to create the `seo_audit_db` database in PostgreSQL and use your actual user and password.*

5.  **Run Database Migrations**

    With your `.env` file configured, run the Alembic migrations to set up your database schema.
    ```bash
    alembic upgrade head
    ```

6.  **Run the Application**

    You need to run two processes in separate terminals: the FastAPI web server and the Celery worker.

    - **Terminal 1: Run FastAPI Server**
      ```bash
      uvicorn app.main:app --reload
      ```
      The API will be available at `http://127.0.0.1:8000`.

    - **Terminal 2: Run Celery Worker**
      ```bash
      celery -A app.celery_app.celery worker --loglevel=info
      ```

### 2. Production Setup with Docker

This method uses Docker and Docker Compose to build and run the entire application stack in isolated containers. This is the recommended approach for production.

**Prerequisites:**
- **Docker** and **Docker Compose**

**Steps:**

1.  **Create the `.env` File for Docker**

    The only difference when using Docker is that the hostnames for the services are the names defined in `docker-compose.yml` (e.g., `postgres-db`, `rabbitmq`).

    ```dotenv
    # .env file for Docker

    BROKER_URL="amqp://guest:guest@rabbitmq:5672//"
    RESULT_BACKEND="postgresql+psycopg://seo_audit_user:your_strong_password@postgres-db:5432/seo_audit_db"
    DATABASE_URL="postgresql+psycopg://seo_audit_user:your_strong_password@postgres-db:5432/seo_audit_db"
    ```

2.  **Build and Run the Containers**
    ```bash
    docker-compose up --build
    ```
    This command builds the images and starts all services. The API will be available at `http://127.0.0.1:8000`.

3.  **Run Database Migrations (in Docker)**

    Open a new terminal and run the migrations inside the running `fastapi-app` container.
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