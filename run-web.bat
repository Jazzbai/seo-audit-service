@echo off
REM This script activates the virtual environment and starts the FastAPI web server.

REM Navigate to the directory where this script is located
cd /d "%~dp0"

REM Activate the virtual environment
call .\venv\Scripts\activate.bat

REM Start the Uvicorn server
echo "Starting Uvicorn web server..."
uvicorn app.main:app --host 0.0.0.0 --port 8000 