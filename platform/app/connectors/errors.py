"""Typed errors raised by ForgeSEO connectors.

The connector layer deliberately keeps errors small and structured.  In
particular, response bodies are not copied into exceptions because a remote
site can echo credentials or other sensitive request data.
"""

from __future__ import annotations

from typing import Any


class ConnectorError(Exception):
    """Base error for a connector operation."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        method: str | None = None,
        url: str | None = None,
        code: str | None = None,
        transport_error: bool = False,
        response_received: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.method = method
        self.url = url
        self.code = code
        self.transport_error = transport_error
        self.response_received = response_received
        self.details = details or {}


class AuthenticationError(ConnectorError):
    """The remote site rejected authentication or credentials are incomplete."""


class IncompleteInventory(ConnectorError):
    """A bounded collection read could not prove that it retrieved every item.

    Messages are local constants, so workers can surface them without copying
    untrusted response text or credentials into activity records.
    """

    MESSAGES = {
        "limit": "Inventory exceeds the pagination limit; collection coverage is incomplete.",
        "pagination": "Inventory pagination is inconsistent; check the site's REST pagination headers and retry.",
        "records": "Inventory returned invalid or repeated records; check the site's REST collection and retry.",
    }

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in self.MESSAGES else "pagination"
        super().__init__(self.MESSAGES[self.reason], code="inventory_incomplete")


class ResourceNotFound(ConnectorError):
    """A requested remote resource does not exist."""


class UnsupportedField(ConnectorError):
    """A caller requested a field the connector cannot prove safe to write."""

    def __init__(self, field: str, message: str | None = None) -> None:
        self.field = field
        super().__init__(message or f"Unsupported connector field: {field}")


class ProtectedField(UnsupportedField):
    """A field is intentionally protected from automated writes."""


class SSRFError(ConnectorError):
    """A URL or redirect is not safe for an outbound connector request."""


class SourceConflict(ConnectorError):
    """The remote editorial source changed after a candidate was prepared."""

    def __init__(
        self,
        expected_hash: str,
        current_hash: str | None,
        *,
        resource_key: str | None = None,
    ) -> None:
        self.expected_hash = expected_hash
        self.current_hash = current_hash
        self.resource_key = resource_key
        label = f" for {resource_key}" if resource_key else ""
        super().__init__(
            f"Remote source conflict{label}: expected {expected_hash}, "
            f"found {current_hash}"
        )


class AmbiguousOutcome(ConnectorError):
    """A write may have reached the remote site but cannot be safely reconciled."""

    def __init__(
        self,
        message: str,
        *,
        operation_key: str | None = None,
        candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        self.operation_key = operation_key
        self.candidates = candidates or []
        super().__init__(message, details={"candidate_count": len(self.candidates)})


# A few integrations historically used the longer spelling.  Keeping aliases
# costs nothing and makes the public error surface stable for callers.
SourceConflictError = SourceConflict
UnsupportedFieldError = UnsupportedField
ProtectedFieldError = ProtectedField
