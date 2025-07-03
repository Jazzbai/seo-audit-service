@echo off
REM This script activates the virtual environment and starts the Celery worker.

REM Navigate to the directory where this script is located
cd /d "%~dp0"

REM Activate the virtual environment
call .\venv\Scripts\activate.bat

REM Start the Celery worker
echo "Starting Celery worker..."
celery -A app.celery_app.celery_app worker -P threads --concurrency=10 --loglevel=info --hostname=seo-audit@%%h 