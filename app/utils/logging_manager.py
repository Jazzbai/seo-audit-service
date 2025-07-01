import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Union

import psutil


class JSONFormatter(logging.Formatter):
    """
    Custom JSON formatter for structured logging.
    Formats log records as JSON with audit context and system metrics.
    """

    def format(self, record: logging.LogRecord) -> str:
        # Base log structure
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "module": record.module,
            "line": record.lineno,
        }

        # Add audit context if available
        if hasattr(record, "audit_id"):
            log_entry["audit_id"] = record.audit_id
        if hasattr(record, "task_name"):
            log_entry["task_name"] = record.task_name
        if hasattr(record, "task_id"):
            log_entry["task_id"] = record.task_id
        if hasattr(record, "worker_id"):
            log_entry["worker_id"] = record.worker_id
        if hasattr(record, "context"):
            log_entry["context"] = record.context
        if hasattr(record, "duration_ms"):
            log_entry["duration_ms"] = record.duration_ms
        if hasattr(record, "memory_mb"):
            log_entry["memory_mb"] = record.memory_mb

        # Add exception information if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": (
                    self.formatException(record.exc_info) if record.exc_info else None
                ),
            }

        return json.dumps(log_entry, ensure_ascii=False)


class LoggingManager:
    """
    Centralized logging manager for structured, per-audit logging.

    Features:
    - Per-audit log files with JSON formatting
    - System-wide logging for FastAPI and Celery
    - Windows-compatible file handling
    - Memory and performance monitoring
    - Future Grafana Loki compatibility
    """

    _instance = None
    _lock = Lock()
    _loggers: Dict[str, logging.Logger] = {}
    _handlers: Dict[str, logging.Handler] = {}

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "_initialized"):
            self._setup_directories()
            self._setup_system_loggers()
            self._initialized = True

    def _setup_directories(self):
        """Create logging directory structure."""
        directories = [
            "logs/workers",
            "logs/system",
            "logs/advertools",  # Ensure advertools directory exists
        ]

        for directory in directories:
            Path(directory).mkdir(parents=True, exist_ok=True)

    def _setup_system_loggers(self):
        """Setup system-wide loggers for FastAPI and Celery."""

        # FastAPI application logger
        app_logger = self._create_system_logger("app", "logs/system/app.log")

        # Celery system logger
        celery_logger = self._create_system_logger("celery", "logs/system/celery.log")

        # Store for easy access
        self._loggers["system.app"] = app_logger
        self._loggers["system.celery"] = celery_logger

    def _create_system_logger(self, name: str, log_file: str) -> logging.Logger:
        """Create a system logger with JSON formatting."""
        logger = logging.getLogger(f"system.{name}")
        logger.setLevel(logging.INFO)

        # Prevent duplicate handlers
        if logger.handlers:
            return logger

        # File handler with JSON formatting
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)

        # Also log to console for development
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(JSONFormatter())
        logger.addHandler(console_handler)

        return logger

    def get_audit_logger(self, audit_id: Union[int, str]) -> logging.Logger:
        """
        Get or create a logger for a specific audit.

        Args:
            audit_id: The audit ID to create logger for

        Returns:
            Logger instance configured for this audit
        """
        logger_key = f"audit.{audit_id}"

        if logger_key in self._loggers:
            return self._loggers[logger_key]

        with self._lock:
            # Double-check pattern to prevent race conditions
            if logger_key in self._loggers:
                return self._loggers[logger_key]

            # Create new audit logger
            logger = logging.getLogger(logger_key)
            logger.setLevel(logging.INFO)

            # Prevent duplicate handlers
            if logger.handlers:
                logger.handlers.clear()

            # File handler for this audit
            log_file = f"logs/workers/worker_{audit_id}.log"
            handler = logging.FileHandler(log_file, encoding="utf-8")
            handler.setFormatter(JSONFormatter())
            logger.addHandler(handler)

            # Store references
            self._loggers[logger_key] = logger
            self._handlers[logger_key] = handler

            return logger

    def log_task_start(
        self,
        audit_id: int,
        task_name: str,
        task_id: str = None,
        context: Dict[str, Any] = None,
        worker_id: str = None,
    ):
        """Log the start of a task with context."""
        logger = self.get_audit_logger(audit_id)

        record = logging.LogRecord(
            name=logger.name,
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=f"Task started: {task_name}",
            args=(),
            exc_info=None,
        )

        # Add custom attributes
        record.audit_id = audit_id
        record.task_name = task_name
        record.task_id = task_id or "unknown"
        record.worker_id = worker_id or self._get_worker_id()
        record.context = context or {}
        record.memory_mb = self._get_memory_usage()

        logger.handle(record)

    def log_task_end(
        self,
        audit_id: int,
        task_name: str,
        task_id: str = None,
        duration_ms: float = None,
        context: Dict[str, Any] = None,
        success: bool = True,
    ):
        """Log the completion of a task with performance metrics."""
        logger = self.get_audit_logger(audit_id)

        level = logging.INFO if success else logging.ERROR
        message = f"Task {'completed' if success else 'failed'}: {task_name}"

        record = logging.LogRecord(
            name=logger.name,
            level=level,
            pathname="",
            lineno=0,
            msg=message,
            args=(),
            exc_info=None,
        )

        # Add custom attributes
        record.audit_id = audit_id
        record.task_name = task_name
        record.task_id = task_id or "unknown"
        record.worker_id = self._get_worker_id()
        record.context = context or {}
        record.duration_ms = duration_ms
        record.memory_mb = self._get_memory_usage()

        logger.handle(record)

    def log_audit_event(
        self,
        audit_id: int,
        level: str,
        message: str,
        context: Dict[str, Any] = None,
        task_name: str = None,
    ):
        """Log a general audit event with context."""
        logger = self.get_audit_logger(audit_id)

        level_num = getattr(logging, level.upper(), logging.INFO)

        record = logging.LogRecord(
            name=logger.name,
            level=level_num,
            pathname="",
            lineno=0,
            msg=message,
            args=(),
            exc_info=None,
        )

        # Add custom attributes
        record.audit_id = audit_id
        if task_name:
            record.task_name = task_name
        record.worker_id = self._get_worker_id()
        record.context = context or {}
        record.memory_mb = self._get_memory_usage()

        logger.handle(record)

    def log_system_event(
        self, system: str, level: str, message: str, context: Dict[str, Any] = None
    ):
        """Log a system-wide event (FastAPI, Celery, etc.)."""
        logger_key = f"system.{system}"
        logger = self._loggers.get(logger_key)

        if not logger:
            # Create system logger if it doesn't exist
            logger = self._create_system_logger(system, f"logs/system/{system}.log")
            self._loggers[logger_key] = logger

        level_num = getattr(logging, level.upper(), logging.INFO)

        record = logging.LogRecord(
            name=logger.name,
            level=level_num,
            pathname="",
            lineno=0,
            msg=message,
            args=(),
            exc_info=None,
        )

        # Add system context
        record.context = context or {}
        record.memory_mb = self._get_memory_usage()

        logger.handle(record)

    def _get_worker_id(self) -> str:
        """Get current worker identifier."""
        try:
            # Try to get Celery worker info if available
            import socket

            hostname = socket.gethostname()
            pid = os.getpid()
            return f"worker@{hostname}:{pid}"
        except (OSError, ImportError):
            return f"worker:{os.getpid()}"

    def _get_memory_usage(self) -> Optional[float]:
        """Get current memory usage in MB."""
        try:
            process = psutil.Process()
            return round(process.memory_info().rss / 1024 / 1024, 2)
        except (psutil.Error, OSError):
            return None

    def cleanup_audit_logger(self, audit_id: Union[int, str]):
        """Clean up resources for a completed audit logger."""
        logger_key = f"audit.{audit_id}"

        with self._lock:
            if logger_key in self._handlers:
                handler = self._handlers[logger_key]
                handler.close()
                del self._handlers[logger_key]

            if logger_key in self._loggers:
                logger = self._loggers[logger_key]
                # Remove all handlers
                for handler in logger.handlers[:]:
                    handler.close()
                    logger.removeHandler(handler)
                del self._loggers[logger_key]


# Global instance
logging_manager = LoggingManager()


class TaskLogger:
    """
    Context manager for task logging with automatic timing and cleanup.

    Usage:
        with TaskLogger(audit_id=42, task_name="crawl", context={"url": "example.com"}):
            # Your task code here
            pass
    """

    def __init__(
        self,
        audit_id: int,
        task_name: str,
        task_id: str = None,
        context: Dict[str, Any] = None,
    ):
        self.audit_id = audit_id
        self.task_name = task_name
        self.task_id = task_id
        self.context = context or {}
        self.start_time = None
        self.logger = logging_manager.get_audit_logger(audit_id)

    def __enter__(self):
        self.start_time = time.time()
        logging_manager.log_task_start(
            audit_id=self.audit_id,
            task_name=self.task_name,
            task_id=self.task_id,
            context=self.context,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.time() - self.start_time) * 1000
        success = exc_type is None

        # Add exception context if there was an error
        if not success and exc_val:
            self.context["error"] = str(exc_val)
            self.context["error_type"] = exc_type.__name__ if exc_type else "Unknown"

        logging_manager.log_task_end(
            audit_id=self.audit_id,
            task_name=self.task_name,
            task_id=self.task_id,
            duration_ms=duration_ms,
            context=self.context,
            success=success,
        )

    def log(self, level: str, message: str, context: Dict[str, Any] = None):
        """Log a message within this task context."""
        merged_context = {**self.context, **(context or {})}
        logging_manager.log_audit_event(
            audit_id=self.audit_id,
            level=level,
            message=message,
            context=merged_context,
            task_name=self.task_name,
        )
