"""Secret-safe, offline checks for the staged external-database deployment.

This does not prove certificate trust, connectivity, firewall rules, backups,
or live readiness. Migrations still have to establish a verified TLS session.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping

from sqlalchemy.engine import make_url

from scripts.preflight import validate_environment


CA_PATH = "/run/forgeseo/db-ca.crt"
ALLOWED_DATABASE_PARAMETERS = {"sslmode", "sslrootcert", "hostaddr", "connect_timeout"}


def validate_split_environment(environ: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    values = dict(environ)
    # Reuse existing strength/distinctness checks using the actual DSN password,
    # never a second, potentially inconsistent DB_PASSWORD variable.
    values["DB_PASSWORD"] = ""
    try:
        database = make_url(values.get("DATABASE_URL", ""))
        if (
            database.drivername != "postgresql+psycopg"
            or not database.host
            or not database.database
            or not database.username
            or not database.password
        ):
            errors.append("DATABASE_URL must specify PostgreSQL/psycopg, host, database and credentials")
        values["DB_PASSWORD"] = database.password or ""
        # Duplicate or overriding libpq parameters must not weaken these checks.
        if any(key not in ALLOWED_DATABASE_PARAMETERS or not isinstance(value, str)
               for key, value in database.query.items()):
            errors.append("DATABASE_URL has unsupported or repeated connection parameters")
        if database.query.get("sslmode") != "verify-full":
            errors.append("DATABASE_URL must use sslmode=verify-full")
        if database.query.get("sslrootcert") != CA_PATH:
            errors.append("DATABASE_URL must use the mounted database CA certificate")
    except (TypeError, ValueError):
        errors.append("DATABASE_URL is missing or malformed")
    except Exception:
        # SQLAlchemy parser exceptions can include the input URL: never echo them.
        errors.append("DATABASE_URL is missing or malformed")

    errors.extend(error.replace("DB_PASSWORD", "DATABASE_URL password")
                  for error in validate_environment(values))
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", values.get("QUEUE_PASSWORD", "")):
        errors.append("QUEUE_PASSWORD must contain at least 32 URL-safe letters, digits, underscores or hyphens")
    if values.get("GLOBAL_PAUSE") != "true":
        errors.append("GLOBAL_PAUSE must remain true during split-host staging")
    for name in ("API_BIND_IP", "TRUSTED_PROXY_IP"):
        try:
            address = ipaddress.ip_address(values.get(name, ""))
            if address.version != 4 or not address.is_private or address.is_loopback or address.is_unspecified:
                raise ValueError
        except ValueError:
            errors.append(f"{name} must be a private, non-loopback IPv4 address")
    return errors


def main() -> int:
    errors = validate_split_environment(os.environ)
    if errors:
        print("ForgeSEO split-host preflight failed:")
        for error in errors:
            print(f"- {error}")
        return 2
    print("Split-host configuration checks passed; remote verification is still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
