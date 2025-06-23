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

## Troubleshooting

### Common Issues and Solutions

#### 1. Database Connection Issues

**Problem:** `getaddrinfo failed` or connection errors during `alembic upgrade head`

**Causes & Solutions:**

**A. URL Encoding Issues with Special Characters in Passwords**

If your PostgreSQL password contains special characters like `@`, `!`, `#`, `%`, these must be URL-encoded in the `DATABASE_URL`.

**Example Problem:**
```env
# This will fail if password contains @ or !
DATABASE_URL=postgresql+psycopg://user:MyP@ssw0rd!@localhost:5432/mydb
```

**Solutions:**

*Option 1: URL-encode the password*
```env
# @ becomes %40, ! becomes %21
DATABASE_URL=postgresql+psycopg://user:MyP%40ssw0rd%21@localhost:5432/mydb
```

*Option 2: Change password to avoid special characters (Recommended)*
```bash
# Connect to PostgreSQL
psql -U postgres -h localhost

# Change to a URL-safe password
ALTER USER seo_audit_user WITH PASSWORD 'MyPasswordStrong123';
```

**B. Service Not Running**

Verify PostgreSQL is running:
```bash
# Windows
Get-Service -Name postgresql*
netstat -an | findstr :5432

# Linux/macOS
sudo systemctl status postgresql
netstat -an | grep :5432
```

#### 2. Database Permission Issues

**Problem:** `permission denied for schema public` during migrations

**Cause:** The database user lacks necessary permissions to create tables and sequences. This is especially common with **PostgreSQL 15+** due to enhanced security defaults.

**Solution:**

1. Connect as PostgreSQL superuser:
   ```bash
   psql -U postgres -h localhost -d your_database_name
   ```

2. **For PostgreSQL 15+ (Enhanced Security):**
   
   First, verify you're connected to the correct database and check current permissions:
   ```sql
   -- Verify database and check current permissions
   SELECT current_database();
   \dn+
   
   -- Check if user exists
   \du
   ```

3. Grant comprehensive permissions:
   ```sql
   -- Grant schema usage and creation permissions
   GRANT USAGE ON SCHEMA public TO your_user;
   GRANT CREATE ON SCHEMA public TO your_user;
   
   -- Grant permissions on existing tables and sequences
   GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO your_user;
   GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO your_user;
   
   -- Grant permissions on future tables and sequences (important!)
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO your_user;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO your_user;
   
   -- Verify permissions were applied (should show UC permissions)
   \dn+
   
   -- Exit
   \q
   ```

4. **Alternative for PostgreSQL 15+ (if above doesn't work):**
   ```sql
   -- Make the user the database owner (gives full permissions)
   ALTER DATABASE your_database_name OWNER TO your_user;
   ```

5. Test and retry the migration:
   ```bash
   # Test if user can create tables
   psql -U your_user -h localhost -d your_database_name -c "CREATE TABLE test_permissions (id INT); DROP TABLE test_permissions;"
   
   # If successful, run migration
   alembic upgrade head
   ```

**PostgreSQL Version Notes:**
- **PostgreSQL 14 and earlier:** Users could CREATE in public schema by default
- **PostgreSQL 15+:** Explicit permissions required for enhanced security
- **Migration Impact:** Existing applications need permission updates when upgrading PostgreSQL

#### 3. Windows Server Deployment Issues

**Problem:** Various encoding or path issues on Windows Server 2019/2022

**Common Issues & Solutions:**

**A. PowerShell Execution Policy**
```powershell
# If you can't activate virtual environment
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

**B. Python Path Issues**
```powershell
# Use the Python Launcher for Windows
py -3.12 -m venv venv
py -3.12 -m pip install -r requirements.txt
```

**C. Console Code Page Warnings**
The warning `Console code page (437) differs from Windows code page (1252)` is cosmetic and won't affect functionality. To suppress:
```powershell
# Set console to UTF-8 (optional)
chcp 65001
```

#### 4. RabbitMQ Connection Issues

**Problem:** Celery can't connect to RabbitMQ

**Solutions:**

**A. Verify RabbitMQ is running:**
```bash
# Windows
Get-Service -Name RabbitMQ
netstat -an | findstr :5672

# Check RabbitMQ management interface (if enabled)
# http://localhost:15672 (guest/guest)
```

**B. Check firewall settings:**
```powershell
# Windows - ensure port 5672 is open
New-NetFirewallRule -DisplayName "RabbitMQ" -Direction Inbound -Port 5672 -Protocol TCP -Action Allow
```

#### 5. Migration History Issues

**Problem:** `alembic_version` table conflicts or "revision not found" errors

**Solutions:**

**A. Check current revision:**
```bash
alembic current
alembic history
```

**B. Reset migration history (if safe):**
```sql
-- Connect to database
psql -U your_user -d your_database

-- Check current version
SELECT * FROM alembic_version;

-- If needed, update to match your latest migration
UPDATE alembic_version SET version_num = 'your_latest_revision_id';
```

**C. Generate new migration if schema differs:**
```bash
alembic revision --autogenerate -m "sync schema"
alembic upgrade head
```

**D. Multiple migration files with table drop issues:**

If you encounter "table does not exist" errors during migration, multiple migration files may be trying to drop the same tables. This commonly happens with auto-generated migrations that include cleanup operations.

*Solution: Make table drops conditional in migration files*
```python
# In the migration file's upgrade() function
def upgrade() -> None:
    # Drop tables only if they exist (for fresh installs)
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    existing_tables = inspector.get_table_names()
    
    if 'table_name' in existing_tables:
        op.drop_table('table_name')
```

#### 6. Port Conflicts

**Problem:** Port 8001 (or your API_PORT) is already in use

**Solution:**

**A. Find what's using the port:**
```bash
# Windows
netstat -ano | findstr :8001

# Kill the process (replace PID)
taskkill /PID <process_id> /F
```

**B. Change the port in `.env`:**
```env
API_PORT=8002
```

#### 7. Virtual Environment Issues

**Problem:** Module import errors or package conflicts

**Solutions:**

**A. Recreate virtual environment:**
```bash
# Deactivate current environment
deactivate

# Remove old environment
rm -rf venv  # Linux/macOS
Remove-Item -Recurse -Force venv  # Windows

# Create new environment
python3.12 -m venv venv  # Linux/macOS
py -3.12 -m venv venv     # Windows

# Activate and reinstall
source venv/bin/activate  # Linux/macOS
.\venv\Scripts\Activate.ps1  # Windows

pip install -r requirements.txt
```

### Getting Help

If you encounter issues not covered here:

1. **Check the logs:** Look at Celery worker logs and FastAPI application logs
2. **Verify environment variables:** Ensure all required variables in `.env` are set correctly
3. **Test database connection:** Use `psql` to verify you can connect with the credentials in your `.env`
4. **Check service status:** Ensure PostgreSQL and RabbitMQ are running and accessible

For production deployments, consider using:
- **Docker Compose** for consistent environments
- **Environment-specific `.env` files** for different deployment stages
- **Database connection pooling** for better performance
- **Process managers** like systemd or PM2 for service management 