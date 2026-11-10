"""Application configuration for the standalone ForgeSEO pilot.

Configuration is deliberately environment-driven. The defaults are suitable
for a local development checkout and do not contain credentials or other
secrets.
"""

from __future__ import annotations

import ipaddress
import json
import re

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


class Settings(BaseSettings):
    """Runtime settings shared by the foundation and application layers."""

    DATABASE_URL: str = "sqlite:///./data/forgeseo.db"
    BROKER_URL: str = "amqp://localhost:5672//"
    ENCRYPTION_KEY: str = ""
    COOKIE_SECURE: bool = False
    GLOBAL_PAUSE: bool = True
    PUBLIC_URL: str = "http://localhost"
    # Closed pilot: both first-owner setup and team additions require an
    # explicit server-side opt-in. Existing users can still sign in.
    ACCOUNT_CREATION_ENABLED: bool = False
    # Required by the deployment for the one-time first-owner bootstrap.
    # An empty local default fails closed; tests and disposable local servers
    # must set an explicit test-only value.
    BOOTSTRAP_TOKEN: str = ""
    ARTIFACT_ROOT: str = "./artifacts"
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = Field(default="", repr=False)
    # Operator-owned configuration, never accepted from site settings or the
    # connection API. Exact public hostname -> fixed internal HTTPS address.
    TRUSTED_HTTPS_ROUTES: str = ""

    @field_validator('TRUSTED_HTTPS_ROUTES')
    @classmethod
    def validate_trusted_https_routes(cls, value: str) -> str:
        routes = json.loads(value) if value.strip() else {}
        if not isinstance(routes, dict) or len(routes) > 16:
            raise ValueError('Trusted HTTPS routes must be an object with at most 16 entries')
        allowed = [ipaddress.ip_network(cidr) for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
        normalized = {}
        for host, address in routes.items():
            host = host.lower().rstrip('.')
            if (len(host) > 253 or host.endswith(('.local', '.internal', '.localhost'))
                    or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host)):
                raise ValueError('Trusted routes require exact public DNS hostnames')
            if not isinstance(address, str):
                raise ValueError('Trusted route addresses must be IPv4 strings')
            ip = ipaddress.IPv4Address(address)
            if not any(ip in network for network in allowed):
                raise ValueError('Trusted routes support RFC1918 IPv4 addresses only')
            if host in normalized:
                raise ValueError('Duplicate trusted hostname')
            normalized[host] = str(ip)
        return json.dumps(normalized, sort_keys=True)

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )


settings = Settings()
