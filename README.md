# SEO Audit Agent

A production-grade, asynchronous SEO audit agent built with FastAPI and Celery. It leverages the `advertools` library to perform robust web crawls and generate comprehensive on-page and link analysis reports with advanced categorization and false positive detection.

The agent is containerized with Docker for easy setup and deployment, providing a simple REST API to trigger audits and retrieve detailed, page-by-page reports in JSON format.

## Key Features

### 🚀 **Core Capabilities**
- **Asynchronous Workflow**: Uses Celery to run audits in the background without blocking the API
- **Robust & Polite Crawling**: Employs `advertools` for efficient website crawling with configurable politeness settings
- **Detailed On-Page SEO Analysis**: Comprehensive page-level analysis including titles, meta descriptions, and heading structure
- **Industry-Standard Error Handling**: Advanced error classification with user-friendly messages

### 🔗 **Advanced Link Analysis**
- **Enhanced Internal Link Categorization**: Detailed analysis of internal links with 5 distinct categories:
  - **Unreachable**: Links that could not be reached due to network errors
  - **Broken**: Links returning definitive `404 Not Found` or `410 Gone` errors
  - **Permission Issues**: Links returning `403 Forbidden` (likely blocking crawlers)
  - **Method Issues**: Links returning `405 Method Not Allowed` (technical incompatibility)
  - **Other Client Errors**: Additional 4xx errors for comprehensive analysis

- **Smart External Link Processing**: 
  - **Asynchronous Processing**: Non-blocking external link checks using chunked async approach
  - **Domain-Aware Chunking**: Intelligent URL grouping to prevent domain collisions and rate limiting
  - **Timeout Protection**: Multi-level timeouts (per-chunk and overall) with graceful degradation
  - **See [ASYNC_EXTERNAL_LINKS_README.md](ASYNC_EXTERNAL_LINKS_README.md) for detailed implementation**

### 🛡️ **False Positive Detection**
- **Industry-Standard Pattern Recognition**: Advanced filtering to reduce false positives from:
  - Authentication-required domains (account subdomains, login pages)
  - Administrative interfaces (/admin/, /dashboard/, /profile/ paths)
  - Cross-domain pattern detection (works for any company, not just specific domains)
  - Enhanced crawler settings with retry logic and proper headers

### 📊 **Enterprise Features**
- **Structured JSON Logging**: Per-audit log files with system metrics and performance tracking
- **Dashboard Integration**: Webhook callbacks for external dashboard systems
- **Recovery Tools**: Automatic detection and recovery of stuck audits
- **Resource Monitoring**: Memory usage tracking and performance optimization
- **RESTful API**: Simple FastAPI interface with comprehensive error handling
- **Persistent Storage**: PostgreSQL with proper audit status tracking and JSON report storage

## Project Architecture

The agent uses a modern microservices architecture with the following components:

1. **FastAPI Application (`fastapi-app`)**: REST API endpoints for audit management
2. **Celery Worker (`celery-worker`)**: Background task processing with multi-stage pipeline
3. **RabbitMQ (`rabbitmq`)**: Message broker for task distribution
4. **PostgreSQL (`postgres-db`)**: Primary database for audit tracking and results
5. **Alembic**: Database schema migration management

### Task Pipeline
```
Start Audit → Crawl Website → Analyze Pages → Check Internal Links → Check External Links (Async) → Generate Report → Send Callbacks
```

## Setup and Running

This project is designed to be run with Docker Compose, which simplifies setup. However, you can also run the services locally for development.

### 1. Recommended Setup with Docker Compose

This is the easiest and recommended way to get started. It runs all services (FastAPI, Celery, Postgres, RabbitMQ) in isolated containers.

**Prerequisites:**
- **Docker** and **Docker Compose**

**Steps:**

1. **Clone the Repository**
   ```bash
   git clone <repository-url>
   cd seo-audit
   ```

2. **Create and Configure the `.env` File**

   Copy the example file to `.env`. This file holds all your configuration.
   ```bash
   cp .env.example .env
   ```
   Open the new `.env` file and **set a secure `API_KEY`**. You can also change the `API_PORT` if the default `8001` conflicts with another service.

   **Optional Dashboard Integration:**
   ```env
   # Dashboard callback settings (optional)
   DASHBOARD_CALLBACK_URL=https://your-dashboard.com/api/audit-results
   DASHBOARD_API_KEY=your-dashboard-api-key
   ```

3. **Build and Run the Containers**
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

1. **Prepare Backing Services**

   You can run PostgreSQL and RabbitMQ natively on your machine, or you can use Docker to easily run them.

   *Using Docker (Recommended for simplicity):*
   ```bash
   docker-compose up -d postgres-db rabbitmq
   ```
   *Manual Installation:* Ensure both services are installed and running on their default ports.

2. **Create the Database and User (Manual PostgreSQL Setup)**
   
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

3. **Create and Activate Virtual Environment**

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

4. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   ```

5. **Configure the `.env` File for Manual Deployment**

   Create the `.env` file if you haven't already (`cp .env.example .env`). **For manual deployment, you must update service hostnames** from Docker container names to localhost.

   **Critical Changes Required:**
   ```env
   # Change from Docker service names to localhost
   DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/database_name
   BROKER_URL=amqp://guest:guest@localhost:5672//
   RESULT_BACKEND=db+postgresql+psycopg://user:password@localhost:5432/database_name
   
   # Set your API configuration
   API_KEY=your-secret-api-key-here
   API_PORT=8001
   ```

   **Why This is Required:**
   - **Docker Compose**: Uses service names (`postgres-db`, `rabbitmq`) for internal networking
   - **Manual Deployment**: Services run on localhost with standard ports
   - **Common Mistake**: Forgetting to change service names causes connection failures

   **Service Name Mapping:**
   - `postgres-db` → `localhost:5432`
   - `rabbitmq` → `localhost:5672`

6. **Run Database Migrations**

   Apply any pending database migrations.
   ```bash
   alembic upgrade head
   ```

7. **Run the Application**

   You need to run two processes in separate terminals (both with the `venv` activated).

   - **Terminal 1: Run FastAPI Server**
     ```bash
     # Port is set directly in the command (not controlled by .env file)
     uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
     ```

   - **Terminal 2: Run Celery Worker**
     ```bash
     # Use unique hostname to avoid conflicts with other Celery workers
     celery -A app.celery_app.celery_app worker -P threads --concurrency=10 --loglevel=info --hostname=seo-audit@%h
     ```

## Using the API

You can interact with the API using tools like `curl`, Postman, or the auto-generated interactive documentation. The API is protected, so you must include your `API_KEY`.

**Interactive Docs**: `http://127.0.0.1:8001/docs` (or your custom port)

### 1. Start a New Audit

```bash
curl -X POST "http://127.0.0.1:8001/v1/audits" \
     -H "X-API-Key: your-api-key-here" \
     -H "Content-Type: application/json" \
     -d '{
       "url": "https://example.com",
       "max_pages": 50,
       "user_id": "optional-user-id",
       "user_audit_report_request_id": "optional-request-id"
     }'
```

**Response:**
```json
{
  "audit_id": 42,
  "task_id": "abc123-def456-ghi789",
  "status": "PENDING"
}
```

### 2. Check Audit Status and Results

```bash
curl -X GET "http://127.0.0.1:8001/v1/audits/42" \
     -H "X-API-Key: your-api-key-here"
```

**Response Structure:**
```json
{
  "audit_id": 42,
  "status": "COMPLETE",
  "url": "https://example.com",
  "user_id": "optional-user-id",
  "user_audit_report_request_id": "optional-request-id",
  "created_at": "2024-01-15T10:30:00Z",
  "completed_at": "2024-01-15T10:35:00Z",
  "error_message": null,
  "technical_error": null,
  "report_json": {
    "summary": {
      "total_pages": 25,
      "pages_with_issues": 8,
      "total_internal_links": 156,
      "internal_broken_links": 2,
      "internal_unreachable_links": 1,
      "internal_permission_issue_links": 0,
      "internal_method_issue_links": 0,
      "internal_other_client_errors": 0,
      "total_external_links": 89,
      "external_broken_links": 3,
      "external_unreachable_links": 2,
      "external_permission_issue_links": 1,
      "external_method_issue_links": 0,
      "external_other_client_errors": 0
    },
    "pages": [
      {
        "url": "https://example.com/page1",
        "title": "Page Title",
        "meta_description": "Page description",
        "h1_count": 1,
        "issues": ["Missing meta description"],
        "internal_links": ["https://example.com/page2"],
        "external_links": ["https://external-site.com"]
      }
    ],
    "broken_internal_links": [
      {
        "url": "https://example.com/missing-page",
        "status": 404,
        "category": "broken",
        "source_pages": ["https://example.com/page1"]
      }
    ],
    "broken_external_links": [
      {
        "url": "https://external-site.com/missing",
        "status": 404,
        "category": "broken",
        "source_pages": ["https://example.com/page1"]
      }
    ]
  }
}
```

## Advanced Features

### 📊 Monitoring and Logging

The application provides comprehensive logging with structured JSON format:

- **Per-Audit Logs**: `logs/workers/worker_{audit_id}.log`
- **System Logs**: `logs/system/app.log` and `logs/system/celery.log`
- **Advertools Logs**: `logs/advertools/audit_log_{audit_id}.log`

**Log Structure:**
```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "INFO",
  "message": "Task started: compile_report_from_crawl",
  "audit_id": 42,
  "task_name": "compile_report_from_crawl",
  "task_id": "abc123-def456",
  "worker_id": "worker-001",
  "memory_mb": 245.6,
  "context": {
    "crawl_output_file": "results/audit_results_42.jl"
  }
}
```

### 🔧 Recovery Tools

If you have stuck audits, you can use the recovery utilities:

```bash
# Check for stuck audits
python recover_stuck_audits.py

# Show all recent audit status  
python recover_stuck_audits.py status

# Auto-recover all stuck audits
python recover_stuck_audits.py auto-recover

# Recover specific audit
python recover_stuck_audits.py recover 42
```

### 🌐 Dashboard Integration

Set up webhook callbacks to send results to external dashboards:

```env
DASHBOARD_CALLBACK_URL=https://your-dashboard.com/api/audit-results
DASHBOARD_API_KEY=your-dashboard-api-key
```

The system will automatically send POST requests with the complete audit results when audits complete.

### ⚡ Performance Optimization

**External Link Processing:**
- Chunked async processing prevents blocking
- Domain-aware distribution prevents rate limiting
- Configurable concurrent processing (default: 3 chunks)
- Automatic timeout handling and partial results

**Memory Management:**
- Per-task memory monitoring
- Automatic cleanup of temporary files
- Resource usage logging

## Troubleshooting

### Common Issues

**1. Stuck Audits**
```bash
# Check status
python recover_stuck_audits.py status

# Auto-recover
python recover_stuck_audits.py auto-recover
```

**2. Connection Issues**
- Verify `.env` file configuration
- Check Docker container status: `docker-compose ps`
- Review logs: `docker-compose logs`

**3. False Positives**
The system automatically detects and filters common false positives from:
- Authentication-required domains
- Administrative interfaces
- Cross-domain restrictions

**4. Memory Issues**
- Monitor logs for memory usage: `grep "memory_mb" logs/workers/*.log`
- Adjust `max_pages` parameter for large sites
- Check available system resources

### Log Analysis

**View Real-Time Logs:**
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f worker

# Specific audit
tail -f logs/workers/worker_42.log
```

**Search Logs:**
```bash
# Find errors
grep "ERROR" logs/system/*.log

# Find specific audit
grep "audit_id.*42" logs/workers/*.log
```

## API Reference

**Base URL:** `http://127.0.0.1:8001` (or your configured port)

**Authentication:** Include `X-API-Key` header with your API key

### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Health check |
| POST | `/v1/audits` | Start new audit |
| GET | `/v1/audits/{audit_id}` | Get audit results |
| GET | `/docs` | Interactive API documentation |

### Request Parameters

**POST /v1/audits:**
- `url` (required): Website URL to audit
- `max_pages` (optional): Maximum pages to crawl (default: 100)
- `user_id` (optional): User identifier for tracking
- `user_audit_report_request_id` (optional): Request tracking ID

### Response Status Codes

- `202`: Audit started successfully
- `200`: Audit results retrieved
- `401`: Invalid or missing API key
- `404`: Audit not found
- `422`: Invalid request parameters

## Contributing

We welcome contributions to the SEO Audit Agent! This project is open source and we encourage community involvement.

### **How to Contribute**

1. **Fork the Repository**
   ```bash
   git clone https://github.com/yourusername/seo-audit.git
   cd seo-audit
   ```

2. **Create a Feature Branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Make Your Changes**
   - Follow the existing code style and patterns
   - Add tests for new functionality
   - Update documentation as needed
   - Ensure all tests pass

4. **Commit Your Changes**
   ```bash
   git commit -m "feat: add your feature description"
   ```
   Follow conventional commit format (feat/fix/docs/refactor/test)

5. **Submit a Pull Request**
   - Provide a clear description of the changes
   - Include any relevant issue numbers
   - Ensure CI/CD checks pass

### **Development Guidelines**

- **Code Style**: Follow PEP 8 for Python code
- **Testing**: Add tests for new features and bug fixes
- **Documentation**: Update README and docstrings for any API changes
- **Logging**: Use the structured logging system for new features
- **Error Handling**: Follow the existing error classification patterns

### **Issues and Feature Requests**
- 🐛 **Bug Reports**: Use the bug report template
- 💡 **Feature Requests**: Use the feature request template  
- 📖 **Documentation**: Help improve our documentation
- 🔧 **Performance**: Optimize existing functionality

### **Development Setup**
Follow the "Manual Local Setup" section above for development environment setup.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For issues and questions:
1. Check the troubleshooting section above
2. Review the logs for detailed error information
3. Use the recovery tools for stuck audits
4. **Create an issue** on GitHub for bugs or feature requests
5. **Join the discussion** in GitHub Discussions for questions

## Acknowledgments

- Built with [advertools](https://github.com/eliasdabbas/advertools) for web crawling
- Uses [FastAPI](https://fastapi.tiangolo.com/) for the REST API
- Powered by [Celery](https://docs.celeryproject.org/) for async task processing
- Thanks to all contributors who help improve this project

---

**Note:** This application is designed for production use with proper error handling, monitoring, and recovery capabilities. The async external link processing and false positive detection make it suitable for auditing large websites without blocking other operations. 