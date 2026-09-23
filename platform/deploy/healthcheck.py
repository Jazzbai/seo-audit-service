"""Small, dependency-light health probes used by production containers.

The API probe checks the application/database contract.  Worker probes use
Celery's control plane and require a pong from the expected worker family;
they do not treat a live container process as proof that work can be consumed.
Beat uses a pid-file probe because it is not a Celery worker and cannot answer
control-plane pings.  Application scheduler freshness remains visible through
the persisted heartbeat exposed by the API.  The pid-file probe also verifies
the process command line so a stale or reused PID cannot report Beat healthy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from celery import Celery


def worker_replies_healthy(replies: object, prefix: str) -> bool:
    """Return true only when the expected worker family answered ``pong``."""

    if not isinstance(replies, dict) or not prefix:
        return False
    for name, response in replies.items():
        if not isinstance(name, str) or not name.startswith(f"{prefix}@"):
            continue
        if isinstance(response, dict) and response.get("ok") == "pong":
            return True
    return False


def check_api(url: str) -> bool:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=float(os.environ.get("HEALTHCHECK_TIMEOUT", "4"))) as response:
        if response.status != 200:
            return False
        body = json.loads(response.read().decode("utf-8"))
    return body.get("status") == "ok"


def check_worker(prefix: str) -> bool:
    broker_url = os.environ.get("BROKER_URL")
    if not broker_url:
        return False
    celery = Celery("forgeseo-healthcheck", broker=broker_url, set_as_current=False)
    celery.conf.update(
        broker_connection_timeout=2,
        broker_connection_max_retries=1,
        broker_connection_retry_on_startup=False,
    )
    inspector = celery.control.inspect(
        timeout=float(os.environ.get("HEALTHCHECK_TIMEOUT", "4")),
    )
    return worker_replies_healthy(inspector.ping(), prefix)


def _read_process_commandline(pid: int) -> tuple[str, ...]:
    """Read a Linux process command line without exposing its arguments."""

    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return tuple(
        argument.decode("utf-8", "strict")
        for argument in raw.split(b"\x00")
        if argument
    )


def _is_celery_beat_process(pid: int) -> bool:
    try:
        commandline = _read_process_commandline(pid)
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return False

    program_names = {
        argument.replace("\\", "/").rsplit("/", 1)[-1]
        for argument in commandline
    }
    return "celery" in program_names and "beat" in commandline


def check_pidfile(pidfile: str) -> bool:
    try:
        pid = int(Path(pidfile).read_text(encoding="ascii").strip())
        if pid <= 1:
            return False
        os.kill(pid, 0)
        return _is_celery_beat_process(pid)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("api", "worker", "process"))
    parser.add_argument("--prefix", default=os.environ.get("CELERY_HEALTH_PREFIX", ""))
    parser.add_argument("--pidfile", default=os.environ.get("CELERY_HEALTH_PIDFILE", ""))
    parser.add_argument(
        "--url",
        default=os.environ.get("HEALTH_URL", "http://127.0.0.1:8000/health"),
    )
    args = parser.parse_args(argv)
    try:
        if args.component == "api":
            healthy = check_api(args.url)
        elif args.component == "worker":
            healthy = check_worker(args.prefix)
        else:
            healthy = check_pidfile(args.pidfile)
    except Exception as exc:  # pragma: no cover - exact broker/library errors vary
        print(f"ForgeSEO health check failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not healthy:
        print(f"ForgeSEO {args.component} health check is not ready", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
