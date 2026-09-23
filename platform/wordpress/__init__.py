"""Optional WordPress-side helper assets for ForgeSEO.

The HTTP clients live under :mod:`app.connectors`; this package exists so the
plugin asset has a stable, discoverable Python-side namespace without making
the client depend on WordPress or PHP.
"""

from app.connectors import WordPressClient

__all__ = ["WordPressClient"]
