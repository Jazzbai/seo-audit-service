"""Safe connector public API with lazy imports.

The network transport is also reusable by other app scopes.  Keeping this
facade lazy prevents importing connector implementations while
``app.network`` is initializing.
"""

from importlib import import_module


_ERROR_EXPORTS = {
    "AmbiguousOutcome",
    "AuthenticationError",
    "ConnectorError",
    "ProtectedField",
    "ProtectedFieldError",
    "ResourceNotFound",
    "SSRFError",
    "SourceConflict",
    "SourceConflictError",
    "UnsupportedField",
    "UnsupportedFieldError",
}
_SECURITY_EXPORTS = {
    "assert_public_url",
    "decrypt_credentials",
    "encrypt_credentials",
    "safe_get",
    "validate_public_origin",
    "validate_public_url",
    "validate_redirect",
}


def __getattr__(name: str):
    if name in _ERROR_EXPORTS:
        return getattr(import_module(".errors", __name__), name)
    if name in _SECURITY_EXPORTS:
        return getattr(import_module(".security", __name__), name)
    if name == "WordPressClient":
        return import_module(".wordpress", __name__).WordPressClient
    if name == "WooCommerceClient":
        return import_module(".woocommerce", __name__).WooCommerceClient
    raise AttributeError(name)

__all__ = [
    "AmbiguousOutcome",
    "AuthenticationError",
    "ConnectorError",
    "ProtectedField",
    "ProtectedFieldError",
    "ResourceNotFound",
    "SSRFError",
    "SourceConflict",
    "SourceConflictError",
    "UnsupportedField",
    "UnsupportedFieldError",
    "WordPressClient",
    "WooCommerceClient",
    "assert_public_url",
    "decrypt_credentials",
    "encrypt_credentials",
    "safe_get",
    "validate_public_origin",
    "validate_public_url",
    "validate_redirect",
]
