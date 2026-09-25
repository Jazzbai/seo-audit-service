"""A conservative async client for the WordPress REST API.

The client uses native ``wp/v2`` resources for ordinary content operations.
SEO writes are only enabled for an explicitly discovered ForgeSEO namespaced
route; the presence of Yoast or Rank Math metadata is read-only evidence, not
permission to write arbitrary plugin fields.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .base import AsyncConnector
from .common import (
    copy_json,
    find_key,
    is_numeric_id_placeholder,
    json_clone,
    operation_key_header,
    raw_field,
    safe_int,
    slugify,
    stable_hash,
    text_value,
    walk_mapping_keys,
)
from .errors import (
    AmbiguousOutcome,
    AuthenticationError,
    ConnectorError,
    IncompleteInventory,
    ProtectedField,
    ResourceNotFound,
    SourceConflict,
    UnsupportedField,
)
from .security import validate_public_url


_WP_CONTENT_TYPES = {
    "post": "posts",
    "posts": "posts",
    "page": "pages",
    "pages": "pages",
    "media": "media",
    "attachment": "media",
    "attachments": "media",
    "author": "users",
    "user": "users",
    "users": "users",
}
_WP_STANDARD_RESOURCE_TYPES = {"post": "posts", "page": "pages"}
_WP_UNSAFE_RESOURCE_BASES = {
    "attachment",
    "attachments",
    "blocks",
    "categories",
    "comments",
    "e-floating-buttons",
    "font-collections",
    "font-families",
    "icon-collections",
    "icons",
    "global-styles",
    "global_styles",
    "media",
    "menu-items",
    "menu_items",
    "menus",
    "navigation",
    "nav-menus",
    "nav_menus",
    "patterns",
    "plugins",
    "settings",
    "statuses",
    "tags",
    "taxonomies",
    "templates",
    "template-parts",
    "template_parts",
    "types",
    "user",
    "users",
}
_WP_UNSAFE_RESOURCE_MARKERS = (
    "elementor",
    "template",
    "pattern",
    "global-style",
    "global_style",
    "menu",
    "navigation",
    "widget",
    "popup",
    "revision",
    "autosave",
    "plugin",
    "setting",
)
_WP_STATUS_VALUES = {"publish", "future", "draft", "pending", "private", "trash", "auto-draft", "inherit"}
_WP_UPDATE_STATUS_VALUES = {"future", "draft", "pending", "private", "trash", "auto-draft", "inherit"}
_SEO_PROVIDERS = {
    "yoast": {"yoast/v1", "yoast/v1/"},
    "rank_math": {"rankmath/v1", "rankmath/v1/", "rank_math/v1", "rank_math/v1/"},
}
_SEO_READ_KEYS = {
    "yoast": {
        "title": "_yoast_wpseo_title",
        "description": "_yoast_wpseo_metadesc",
        "focus_keyword": "_yoast_wpseo_focuskw",
        "canonical_url": "_yoast_wpseo_canonical",
    },
    "rank_math": {
        "title": "rank_math_title",
        "description": "rank_math_description",
        "focus_keyword": "rank_math_focus_keyword",
        "canonical_url": "rank_math_canonical_url",
    },
    "forgeseo": {
        "title": "_forgeseo_seo_title",
        "description": "_forgeseo_seo_description",
        "focus_keyword": "_forgeseo_focus_keyword",
        "canonical_url": "_forgeseo_canonical_url",
    },
}
_FORBIDDEN_KEYS = {
    "template",
    "page_template",
    "builder",
    "builder_data",
    "page_builder",
    "layout",
    "layout_data",
    "elementor",
    "elementor_data",
    "_elementor_data",
    "divi",
    "bricks",
    "avada",
    "oxygen",
    "wpbakery",
    "visual_composer",
    "acf",
    "custom_fields",
    "meta",
    "meta_data",
    "price",
    "regular_price",
    "sale_price",
    "stock",
    "stock_quantity",
    "stock_status",
    "manage_stock",
    "sku",
    "global_unique_id",
    "variations",
    "commerce",
}
_FORBIDDEN_KEY_NORMALIZED = {key.lower().replace("-", "_") for key in _FORBIDDEN_KEYS}
_SEO_FIELDS = {"title", "description", "focus_keyword", "canonical_url"}
_SEO_WRITE_METHODS = {"POST", "PUT", "PATCH"}
_EDITORIAL_FIELDS = (
    "title",
    "body",
    "excerpt",
    "slug",
    "status",
    "author",
    "featured_media",
    "categories",
    "tags",
)
_WP_RESOURCE_WRITE_PERMISSIONS = {
    "posts": {
        "create": "edit_posts",
        "update": "edit_posts",
        "publish": "publish_posts",
    },
    "pages": {
        "create": "edit_pages",
        "update": "edit_pages",
        "publish": "publish_pages",
    },
}

# These are the metadata keys that the connector can identify without
# fetching or modifying a page.  WordPress exposes registered REST metadata in
# the route argument/schema document, which makes this safe for onboarding
# discovery and keeps the decision separate from the per-resource guard in
# ``_builder_present`` below.
_BUILDER_METADATA_FIELDS = {
    "elementor": {
        "_elementor_data",
        "elementor_data",
        "_elementor_edit_mode",
        "elementor_edit_mode",
    },
    "divi": {
        "_et_pb_use_builder",
        "et_pb_use_builder",
        "_et_pb_page_layout",
        "_et_pb_old_content",
    },
    "bricks": {
        "_bricks_page_content_2",
        "_bricks_page_content",
    },
    "beaver_builder": {
        "_fl_builder_data",
        "_fl_builder_enabled",
    },
    "wpbakery": {
        "_wpb_vc_js_status",
        "_wpb_vc_js_interface",
    },
    "oxygen": {
        "_ct_builder_shortcodes",
        "ct_builder_shortcodes",
    },
}
_GENERIC_BUILDER_METADATA_FIELDS = {
    "builder",
    "builder_data",
    "page_builder",
    "layout_data",
}
_BUILDER_FIELD_PREFIXES = {
    "elementor": ("elementor", "_elementor"),
    "divi": ("divi", "_divi", "et_pb", "_et_pb"),
    "bricks": ("bricks", "_bricks"),
    "beaver_builder": ("beaver", "_fl_builder", "fl_builder"),
    "wpbakery": ("wpbakery", "_wpb", "wpb_", "vc_"),
    "oxygen": ("oxygen", "_ct_", "ct_"),
}


def _route_methods(route_info: Mapping[str, Any]) -> set[str]:
    methods: set[str] = set()
    direct = route_info.get("methods")
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes)):
        methods.update(str(method).upper() for method in direct)
    endpoints = route_info.get("endpoints")
    if isinstance(endpoints, Sequence) and not isinstance(endpoints, (str, bytes)):
        for endpoint in endpoints:
            if isinstance(endpoint, Mapping):
                endpoint_methods = endpoint.get("methods")
                if isinstance(endpoint_methods, Sequence) and not isinstance(endpoint_methods, (str, bytes)):
                    methods.update(str(method).upper() for method in endpoint_methods)
    return methods


def _route_args(route_info: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    direct = route_info.get("args")
    if isinstance(direct, Mapping):
        names.update(str(name) for name in direct)
    endpoints = route_info.get("endpoints")
    if isinstance(endpoints, Sequence) and not isinstance(endpoints, (str, bytes)):
        for endpoint in endpoints:
            if isinstance(endpoint, Mapping) and isinstance(endpoint.get("args"), Mapping):
                names.update(str(name) for name in endpoint["args"])
    return names


def _route_args_for_methods(route_info: Mapping[str, Any], methods: set[str]) -> set[str]:
    """Return arguments advertised by endpoints supporting ``methods``.

    WordPress route documents commonly put GET and POST schemas beside each
    other.  The old discovery code merged all arguments, which could report a
    field as writable merely because it appeared on a read endpoint.  Keep
    this helper deliberately narrow so it is suitable for authorization
    decisions.
    """

    wanted = {method.upper() for method in methods}
    names: set[str] = set()
    direct_methods = _route_methods({"methods": route_info.get("methods")})
    direct_args = route_info.get("args")
    if direct_methods.intersection(wanted) and isinstance(direct_args, Mapping):
        names.update(str(name) for name in direct_args)

    endpoints = route_info.get("endpoints")
    if isinstance(endpoints, Sequence) and not isinstance(endpoints, (str, bytes)):
        for endpoint in endpoints:
            if not isinstance(endpoint, Mapping):
                continue
            endpoint_methods = _route_methods({"methods": endpoint.get("methods")})
            endpoint_args = endpoint.get("args")
            if endpoint_methods.intersection(wanted) and isinstance(endpoint_args, Mapping):
                names.update(str(name) for name in endpoint_args)
    return names


def _builder_provider_for_field(field: str) -> str | None:
    normalized = field.lower().replace("-", "_")
    for provider, fields in _BUILDER_METADATA_FIELDS.items():
        if normalized in {item.lower().replace("-", "_") for item in fields}:
            return provider
    # A custom registered key can still identify a builder when it starts with
    # an explicit provider marker. Avoid arbitrary substring matching: normal
    # commerce keys such as ``sold_individually`` contain ``divi`` by chance.
    for provider, prefixes in _BUILDER_FIELD_PREFIXES.items():
        if any(normalized.startswith(prefix) for prefix in prefixes):
            return provider
    return None


def _field_text(value: Any) -> str:
    return text_value(value)


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


class WordPressClient(AsyncConnector):
    """Async WordPress REST client with source-hash guarded writes."""

    def __init__(
        self,
        origin: str,
        credentials: dict[str, object],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(origin, credentials, transport, auth_kind="wordpress")
        self._api_root = f"{self.origin.rstrip('/')}/wp-json"
        self._discovery: dict[str, Any] | None = None
        self._routes: dict[str, Mapping[str, Any]] = {}
        self._resource_routes: dict[str, str] = {}
        self._resource_types: list[dict[str, Any]] = []
        self._resource_type_by_key: dict[str, dict[str, Any]] = {}
        self._resource_type_by_base: dict[str, dict[str, Any]] = {}
        self._available_statuses: list[str] = []
        self._seo_provider: str | None = None
        self._forgeseo_fields: set[str] = set()
        self._forgeseo_seo_methods: set[str] = set()
        self._forgeseo_seo_route: str | None = None
        self._forgeseo_operation_route: str | None = None

    def _wp_path(self, path: str = "") -> str:
        if not path:
            return f"{self._api_root}/"
        return f"{self._api_root}/{path.lstrip('/')}"

    def _route(self, endpoint: str) -> Mapping[str, Any] | None:
        target = "/" + endpoint.strip("/")
        exact = self._routes.get(target) or self._routes.get(target + "/")
        if exact is not None:
            return exact
        for route, info in self._routes.items():
            candidate = route.rstrip("/")
            if candidate == target.rstrip("/"):
                return info
        return None

    def _supports(self, endpoint: str, method: str | None = None) -> bool:
        info = self._route(endpoint)
        if info is None:
            # A namespace response can omit route schemas on older installs.
            # It is enough evidence for read-only discovery, but not for a
            # mutation: a collection POST must be explicitly documented.
            if method is None:
                return "wp/v2" in (self._discovery or {}).get("namespaces", [])
            return (
                method.upper() in {"GET", "HEAD", "OPTIONS"}
                and "wp/v2" in (self._discovery or {}).get("namespaces", [])
            )
        if method is None:
            return True
        return method.upper() in _route_methods(info)

    def _supports_item(self, endpoint: str, method: str) -> bool:
        """Check the numeric item route used for an update or publication."""

        _, info = self._item_route_info(endpoint)
        return info is not None and method.upper() in _route_methods(info)

    def _item_route_info(
        self, endpoint: str
    ) -> tuple[str | None, Mapping[str, Any] | None]:
        """Return an exact numeric item route, never a nested route."""

        base = "/" + endpoint.strip("/")
        item_pattern = re.compile(
            rf"^{re.escape(base)}/(?P<placeholder>\(\?P<[^>]+>[^)]+\)|\{{[^}}]+\}})/?$"
        )
        for route in sorted(self._routes):
            info = self._routes[route]
            route_clean = route.rstrip("/")
            match = item_pattern.fullmatch(route_clean)
            if match and is_numeric_id_placeholder(match.group("placeholder")):
                return route, info
        return None, None

    @staticmethod
    def _simple_rest_base(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        candidate = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", candidate):
            return None
        return candidate

    @staticmethod
    def _resource_is_safe(rest_base: str) -> bool:
        if rest_base in _WP_UNSAFE_RESOURCE_BASES:
            return False
        return not any(marker in rest_base for marker in _WP_UNSAFE_RESOURCE_MARKERS)

    def _collection_route_info(
        self, rest_base: str
    ) -> tuple[str | None, Mapping[str, Any] | None]:
        target = f"/wp/v2/{rest_base}"
        for route in sorted(self._routes):
            if route.rstrip("/") == target:
                return route, self._routes[route]
        return None, None

    @staticmethod
    def _route_capability(
        route: str | None,
        info: Mapping[str, Any] | None,
        *,
        fallback_read: bool = False,
    ) -> dict[str, Any]:
        methods = _route_methods(info or {})
        read_methods = sorted(method for method in methods if method in {"GET", "HEAD"})
        write_methods = sorted(method for method in methods if method in {"POST", "PUT", "PATCH"})
        return {
            "route": route,
            "read": bool(read_methods) or fallback_read,
            "write": bool(write_methods),
            "read_methods": read_methods,
            "write_methods": write_methods,
        }

    @staticmethod
    def _explicit_route_viewable(info: Mapping[str, Any] | None) -> bool | None:
        """Read an optional viewability marker without guessing from a route."""

        if not isinstance(info, Mapping):
            return None
        for key in ("viewable", "viewable_in_rest", "show_in_rest"):
            value = info.get(key)
            if isinstance(value, bool):
                return value
        endpoints = info.get("endpoints")
        if isinstance(endpoints, Sequence) and not isinstance(endpoints, (str, bytes)):
            for endpoint in endpoints:
                if isinstance(endpoint, Mapping):
                    for key in ("viewable", "viewable_in_rest", "show_in_rest"):
                        value = endpoint.get(key)
                        if isinstance(value, bool):
                            return value
        return None

    def _resource_descriptor(
        self,
        *,
        key: str,
        name: str,
        label: str,
        rest_base: str,
        viewable: bool | None,
        source: str,
        configured: bool = False,
    ) -> dict[str, Any] | None:
        """Build a safe, non-secret capability record for one post type."""

        normalized_key = self._simple_rest_base(key)
        normalized_base = self._simple_rest_base(rest_base)
        if normalized_key is None or normalized_base is None:
            return None
        if not self._resource_is_safe(normalized_base) or not self._resource_is_safe(normalized_key):
            return None

        collection_route, collection_info = self._collection_route_info(normalized_base)
        item_route, item_info = self._item_route_info(f"/wp/v2/{normalized_base}")
        is_standard = normalized_key in _WP_STANDARD_RESOURCE_TYPES
        # A type descriptor can advertise a nested REST base (for example a
        # font-face child route) that is not a collection this connector can
        # safely enumerate. Do not surface it as an apparently available
        # resource with fabricated route metadata.
        if not is_standard and (collection_route is None or item_route is None):
            return None
        collection = self._route_capability(
            collection_route,
            collection_info,
            fallback_read=(
                self._supports(f"/wp/v2/{normalized_base}", "GET")
                if is_standard
                else False
            ),
        )
        item = self._route_capability(item_route, item_info)
        routes_are_safe = bool(collection["read"] and item["read"])
        inventory_routes_are_safe = bool(collection["read"] if is_standard else routes_are_safe)
        inventoryable = bool(
            inventory_routes_are_safe
            and (
                is_standard
                or viewable is True
                or configured
            )
        )
        if is_standard:
            write_supported = bool("POST" in item["write_methods"])
            write_policy = "native_standard_type"
            write_reason = None
        else:
            # The route may technically accept a mutation, but the generic
            # connector has no type-specific write contract for arbitrary CPTs.
            # Keep the raw route capability visible while refusing to advertise
            # it as an automatic ForgeSEO editorial write surface.
            write_supported = False
            write_policy = "inventory_only"
            write_reason = "Custom post types require an explicit connector write contract"

        return {
            "key": normalized_key,
            "name": str(name or normalized_key),
            "label": str(label or name or normalized_key),
            "rest_base": normalized_base,
            "viewable": viewable if viewable is not None else False,
            "source": source,
            "collection": collection,
            "item": item,
            "inventoryable": inventoryable,
            "editorial_write": {
                "supported": write_supported,
                "automatic": write_supported,
                "policy": write_policy,
                "reason": write_reason,
            },
        }

    def _route_resource_candidates(self) -> dict[str, dict[str, Any]]:
        """Find simple wp/v2 collection/item pairs without trusting nested routes."""

        candidates: dict[str, dict[str, Any]] = {}
        collection_pattern = re.compile(r"^/wp/v2/(?P<base>[^/]+)/?$")
        for route in sorted(self._routes):
            match = collection_pattern.fullmatch(route)
            if not match:
                continue
            rest_base = self._simple_rest_base(match.group("base"))
            if rest_base is None or not self._resource_is_safe(rest_base):
                continue
            if self._item_route_info(f"/wp/v2/{rest_base}")[0] is None:
                continue
            viewable = self._explicit_route_viewable(self._routes[route])
            candidates[rest_base] = {
                "key": rest_base,
                "name": rest_base,
                "label": rest_base,
                "rest_base": rest_base,
                "viewable": viewable,
                "source": "routes",
            }
        return candidates

    async def _discover_resource_types(self) -> list[dict[str, Any]]:
        """Cache REST-visible post types and their safe route capabilities."""

        type_metadata: dict[str, Mapping[str, Any]] = {}
        source = "routes"
        types_route = self._route("/wp/v2/types")
        if types_route is not None and "GET" in _route_methods(types_route):
            # The authenticated edit context includes the reliable `viewable`
            # flag on WordPress installations that omit it from `view`. If a
            # site refuses that context, fall back to the public shape without
            # allowing a failed probe to block standard post/page discovery.
            for context in ("edit", "view"):
                try:
                    payload, _ = await self._json_request(
                        "GET",
                        self._wp_path("wp/v2/types"),
                        params={"context": context},
                    )
                except ConnectorError:
                    continue
                if isinstance(payload, Mapping):
                    source = "types_endpoint"
                    for raw_key, raw_value in payload.items():
                        if isinstance(raw_value, Mapping):
                            type_metadata[str(raw_key)] = raw_value
                    break

        candidates = self._route_resource_candidates()
        descriptors: dict[str, dict[str, Any]] = {}

        for raw_key, metadata in type_metadata.items():
            key = self._simple_rest_base(metadata.get("slug") or raw_key)
            if key is None:
                continue
            rest_base = self._simple_rest_base(metadata.get("rest_base")) or (
                _WP_STANDARD_RESOURCE_TYPES.get(key) or key
            )
            labels = metadata.get("labels")
            label = (
                labels.get("name")
                if isinstance(labels, Mapping) and isinstance(labels.get("name"), str)
                else metadata.get("name")
            )
            descriptor = self._resource_descriptor(
                key=key,
                name=str(metadata.get("name") or key),
                label=str(label or key),
                rest_base=rest_base,
                viewable=(True if key in _WP_STANDARD_RESOURCE_TYPES else metadata.get("viewable") is True),
                source=source,
            )
            if descriptor is not None:
                descriptors[key] = descriptor

        # Standard post/page inventory remains available on older or unusual
        # installs even when /wp/v2/types is absent or malformed.
        for key, rest_base in _WP_STANDARD_RESOURCE_TYPES.items():
            if key in descriptors:
                continue
            descriptor = self._resource_descriptor(
                key=key,
                name=key,
                label="Posts" if key == "post" else "Pages",
                rest_base=rest_base,
                viewable=True,
                source="routes",
            )
            if descriptor is not None:
                descriptors[key] = descriptor

        # Route-only custom candidates remain visible only when their route
        # metadata explicitly marks them viewable.  A mere route pair is not
        # enough to distinguish a CPT from a taxonomy or an internal endpoint.
        for rest_base, candidate in candidates.items():
            key = str(candidate["key"])
            if key in descriptors:
                continue
            if candidate.get("viewable") is not True:
                continue
            descriptor = self._resource_descriptor(
                key=key,
                name=str(candidate["name"]),
                label=str(candidate["label"]),
                rest_base=rest_base,
                viewable=True,
                source="routes",
            )
            if descriptor is not None:
                descriptors[key] = descriptor

        configured_types = self.credentials.get("post_types", [])
        if isinstance(configured_types, Sequence) and not isinstance(configured_types, (str, bytes)):
            for raw_type in configured_types:
                if not isinstance(raw_type, str):
                    continue
                configured_key = self._simple_rest_base(raw_type)
                if configured_key is None:
                    continue
                descriptor = descriptors.get(configured_key)
                if descriptor is None:
                    descriptor = next(
                        (
                            item
                            for item in descriptors.values()
                            if item.get("rest_base") == configured_key
                        ),
                        None,
                    )
                if descriptor is not None:
                    descriptor["inventoryable"] = bool(
                        descriptor["collection"]["read"]
                        if descriptor["key"] in _WP_STANDARD_RESOURCE_TYPES
                        else descriptor["collection"]["read"] and descriptor["item"]["read"]
                    )
                    descriptor["source"] = "configured"
                    continue
                candidate = candidates.get(configured_key)
                if candidate is None:
                    continue
                descriptor = self._resource_descriptor(
                    key=configured_key,
                    name=str(candidate["name"]),
                    label=str(candidate["label"]),
                    rest_base=str(candidate["rest_base"]),
                    viewable=candidate.get("viewable"),
                    source="configured",
                    configured=True,
                )
                if descriptor is not None:
                    descriptors[configured_key] = descriptor

        self._resource_types = [descriptors[key] for key in sorted(descriptors)]
        self._resource_type_by_key = {
            descriptor["key"]: descriptor for descriptor in self._resource_types
        }
        self._resource_type_by_base = {
            descriptor["rest_base"]: descriptor for descriptor in self._resource_types
        }
        return copy_json(self._resource_types)

    @staticmethod
    def _seo_route_resource(route: str) -> str | None:
        """Return the resource for a verified, item-scoped SEO route.

        The REST index is remote input.  A route merely containing ``seo`` is
        not enough evidence that it targets the current post or page: a
        plugin can also expose collection or site-settings endpoints.  Only
        the connector's documented item shape is safe to use here.
        """

        normalized = "/" + str(route).strip("/")
        match = re.fullmatch(
            r"/forgeseo/v1/(?P<resource>posts|pages)/"
            r"(?P<placeholder>\(\?P<id>[^)]+\)|\{id\})/seo/?",
            normalized,
            re.IGNORECASE,
        )
        if match is None:
            return None

        if not is_numeric_id_placeholder(match.group("placeholder")):
            return None
        return match.group("resource").lower()

    def _ensure_item_write_capability(self, endpoint: str) -> None:
        """Fail closed when the selected resource has no documented item write route."""

        if not self._supports_item(f"/wp/v2/{endpoint}", "POST"):
            raise ConnectorError(
                f"WordPress {self._resource_type(endpoint)} item write capability is not available"
            )

    def _builder_constraints(self) -> dict[str, Any]:
        """Describe builder metadata found in the published REST schemas."""

        fields_by_provider: dict[str, set[str]] = {}

        def visit(value: Any, *, metadata_context: bool = False) -> None:
            if isinstance(value, Mapping):
                for raw_key, child in value.items():
                    field = str(raw_key)
                    normalized = field.lower().replace("-", "_")
                    provider = _builder_provider_for_field(field)
                    generic = normalized in _GENERIC_BUILDER_METADATA_FIELDS
                    if provider is not None or (metadata_context and generic):
                        name = provider or "generic_builder"
                        fields_by_provider.setdefault(name, set()).add(field)
                    child_is_metadata = metadata_context or normalized in {
                        "meta",
                        "metadata",
                        "custom_fields",
                        "customfields",
                    }
                    visit(child, metadata_context=child_is_metadata)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for child in value:
                    visit(child, metadata_context=metadata_context)

        for route_info in self._routes.values():
            visit(route_info)

        metadata_fields = sorted(
            field for fields in fields_by_provider.values() for field in fields
        )
        providers = sorted(fields_by_provider)
        detected = bool(metadata_fields)
        return {
            "detected": detected,
            "providers": providers,
            "metadata_fields": metadata_fields,
            "detection_source": "wordpress_rest_route_metadata",
            "metadata_write": False,
            "body_write": {
                "mode": "protected_when_builder_metadata_present" if detected else "native",
                "allowed_for_unmanaged_target": True,
                "blocked_for_builder_target": detected,
                "requires_target_check": detected,
            },
            "protected_fields": ["body", "content"] if detected else [],
        }

    @staticmethod
    def _seo_provider_for_plugins(plugins: Mapping[str, Mapping[str, Any]]) -> str | None:
        if plugins.get("forgeseo", {}).get("write"):
            return "forgeseo"
        read_only = [
            provider
            for provider in ("yoast", "rank_math")
            if plugins.get(provider, {}).get("detected")
        ]
        if len(read_only) == 1:
            return read_only[0]
        if len(read_only) == 0 and plugins.get("forgeseo", {}).get("read"):
            return "forgeseo"
        return None

    def _detect_plugins(self, namespaces: set[str]) -> dict[str, Any]:
        self._forgeseo_fields = set()
        self._forgeseo_seo_methods = set()
        self._forgeseo_seo_route = None
        self._forgeseo_operation_route = None
        detected: dict[str, Any] = {}
        for provider, names in _SEO_PROVIDERS.items():
            found = any(namespace.rstrip("/").lower() in {name.rstrip("/").lower() for name in names} for namespace in namespaces)
            detected[provider] = {
                "detected": found,
                "read": found,
                "write": False,
                "writable_fields": [],
            }

        # The ForgeSEO plugin is intentionally the only recognized SEO write
        # surface.  Its REST schema must advertise both a namespaced SEO route
        # and the exact field names before a write is enabled.
        for route, info in self._routes.items():
            route_lower = route.lower()
            if not route_lower.startswith("/forgeseo/v1/"):
                continue
            methods = _route_methods(info)
            if self._seo_route_resource(route) is not None:
                # Keep the fields tied to the selected route *and method*. A
                # route can advertise POST and PUT endpoints with different
                # schemas; reporting their union while selecting one verb
                # would overstate what one write can actually perform.
                write_candidates: list[tuple[str, set[str]]] = []
                for method in ("POST", "PUT", "PATCH"):
                    if method not in methods:
                        continue
                    fields = {
                        field.lower().replace("-", "_")
                        for field in _route_args_for_methods(info, {method})
                    }.intersection(_SEO_FIELDS)
                    if fields:
                        write_candidates.append((method, fields))
                if write_candidates:
                    method, fields = max(
                        write_candidates,
                        key=lambda candidate: len(candidate[1]),
                    )
                    if len(fields) > len(self._forgeseo_fields):
                        self._forgeseo_fields = fields
                        self._forgeseo_seo_methods = {method}
                        self._forgeseo_seo_route = route
            if "operation" in route_lower and "GET" in methods:
                self._forgeseo_operation_route = route

        forgeseo_namespace = any(namespace.rstrip("/").lower() == "forgeseo/v1" for namespace in namespaces)
        forgeseo_route = any(route.lower().startswith("/forgeseo/v1/") for route in self._routes)
        forgeseo_read_route = any(
            route.lower().startswith("/forgeseo/v1/") and "GET" in _route_methods(info)
            for route, info in self._routes.items()
        )
        forgeseo_detected = forgeseo_namespace or forgeseo_route
        detected["forgeseo"] = {
            "detected": forgeseo_detected,
            "read": forgeseo_namespace or forgeseo_read_route,
            "write": bool(self._forgeseo_fields and self._forgeseo_seo_route),
            "writable_fields": sorted(self._forgeseo_fields),
            "operation_mapping": bool(self._forgeseo_operation_route),
            "webhooks": False,
        }
        return detected

    async def discover(self) -> dict[str, Any]:
        """Discover native and explicitly namespaced capabilities."""

        if self._discovery is not None:
            return json_clone(self._discovery["capabilities"])

        response: httpx.Response | None = None
        try:
            payload, response = await self._json_request("GET", self._wp_path(""))
        except ConnectorError as exc:
            # Pretty-permalink-disabled sites advertise the API from a HEAD
            # response.  This fallback remains GET/REST-only after discovery.
            if exc.status_code != 404:
                raise
            head = await self._request_response("HEAD", self.origin)
            link_header = head.headers.get("link", "")
            match = re.search(r"<([^>]+)>\s*;\s*rel=[\"']https://api\.w\.org/[\"']", link_header, re.I)
            if not match:
                raise ConnectorError("WordPress REST API discovery failed", status_code=404) from exc
            discovered_url = validate_public_url(match.group(1), resolve_dns=self._resolve_dns)
            payload, response = await self._json_request("GET", discovered_url)

        if not isinstance(payload, Mapping):
            raise ConnectorError("WordPress API discovery returned an invalid document")
        namespaces = {
            str(namespace).rstrip("/")
            for namespace in payload.get("namespaces", [])
            if isinstance(namespace, str)
        }
        routes_value = payload.get("routes")
        self._routes = {
            str(route): info
            for route, info in (routes_value.items() if isinstance(routes_value, Mapping) else [])
            if isinstance(info, Mapping)
        }
        if any(route.startswith("/wp/v2/") for route in self._routes):
            namespaces.add("wp/v2")
        self._discovery = {
            "name": payload.get("name") if isinstance(payload.get("name"), str) else None,
            "namespaces": sorted(namespaces),
            "routes": copy_json(self._routes),
            "authentication": copy_json(payload.get("authentication", {})) if isinstance(payload.get("authentication", {}), Mapping) else {},
        }
        self._available_statuses = self._discover_status_names()
        plugins = self._detect_plugins(namespaces)
        resource_types = await self._discover_resource_types()
        connector_capabilities: Mapping[str, Any] = {}
        capabilities_route = self._route("/forgeseo/v1/capabilities")
        if capabilities_route is not None and "GET" in _route_methods(capabilities_route):
            connector_payload, _ = await self._json_request(
                "GET",
                self._wp_path("forgeseo/v1/capabilities"),
            )
            if not isinstance(connector_payload, Mapping):
                raise ConnectorError("ForgeSEO connector returned invalid capabilities")
            connector_capabilities = connector_payload

            advertised_fields = connector_capabilities.get("seo_fields")
            if isinstance(advertised_fields, Sequence) and not isinstance(advertised_fields, (str, bytes)):
                connector_fields = {
                    str(field).lower().replace("-", "_")
                    for field in advertised_fields
                    if isinstance(field, str)
                }
                self._forgeseo_fields.intersection_update(connector_fields)
            if connector_capabilities.get("seo_write_supported") is not True:
                self._forgeseo_fields = set()
                self._forgeseo_seo_methods = set()
                self._forgeseo_seo_route = None

            plugins["forgeseo"]["webhooks"] = connector_capabilities.get("webhooks") is True

        plugins["forgeseo"]["write"] = bool(
            self._forgeseo_fields and self._forgeseo_seo_route
        )
        plugins["forgeseo"]["writable_fields"] = sorted(self._forgeseo_fields)
        self._seo_provider = self._seo_provider_for_plugins(plugins)

        wp_v2 = "wp/v2" in namespaces
        native = {
            "read": wp_v2 and self._supports("/wp/v2/posts", "GET"),
            "create": wp_v2 and self._supports("/wp/v2/posts", "POST"),
            "update": wp_v2 and self._supports_item("/wp/v2/posts", "POST"),
            "publish": wp_v2 and self._supports_item("/wp/v2/posts", "POST"),
            "pages": wp_v2 and self._supports("/wp/v2/pages", "GET"),
            "media": wp_v2 and self._supports("/wp/v2/media", "GET"),
            "authors": wp_v2 and self._supports("/wp/v2/users", "GET"),
            "statuses": wp_v2 and self._supports("/wp/v2/statuses", "GET"),
        }
        builder_constraints = self._builder_constraints()
        editorial_constraints = {
            "read": native["read"],
            "write": native["update"],
            "writable_fields": list(_EDITORIAL_FIELDS) if native["update"] else [],
            "conditionally_writable_fields": ["body"]
            if native["update"] and builder_constraints["detected"]
            else [],
            "protected_fields": ["metadata", *builder_constraints["protected_fields"]],
            "builder_constraints": builder_constraints,
        }
        self._discovery["capabilities"] = {
            "kind": "wordpress",
            "rest_api": True,
            "wp_v2": wp_v2,
            "native": native,
            "resource_types": resource_types,
            "editorial": editorial_constraints,
            "plugins": plugins,
            "seo": {
                "provider": self._seo_provider,
                "active_provider": connector_capabilities.get("seo_provider"),
                "read": any(plugin["read"] for plugin in plugins.values()),
                "write": plugins["forgeseo"]["write"],
                "writable_fields": sorted(self._forgeseo_fields),
            },
            # Flat aliases are useful to UI capability consumers and preserve
            # a stable JSON shape without exposing credentials.
            "yoast": bool(plugins["yoast"]["detected"]),
            "rank_math": bool(plugins["rank_math"]["detected"]),
            "forgeseo_plugin": plugins["forgeseo"],
        }
        return json_clone(self._discovery["capabilities"])

    async def validate_connection(self) -> dict[str,Any]:
        capabilities = await self.discover()
        user, _ = await self._json_request(
            'GET',
            self._wp_path('wp/v2/users/me'),
            params={'context': 'edit'},
        )
        if not isinstance(user, Mapping) or not user.get('id'):
            raise ConnectorError('WordPress did not verify an authenticated user')
        permissions = user.get('capabilities', {})
        if not isinstance(permissions, Mapping):
            permissions = {}
        authenticated_permissions = {
            endpoint: {
                action: permissions.get(capability) is True
                for action, capability in action_permissions.items()
            }
            for endpoint, action_permissions in _WP_RESOURCE_WRITE_PERMISSIONS.items()
        }
        # The legacy native fields describe the posts endpoint.  Keep that
        # stable for existing consumers while exposing resource-scoped page
        # permissions separately so a page editor is neither over- nor
        # under-authorized by a posts-only check.
        native = dict(capabilities['native'])
        native['create'] = bool(
            native['create'] and authenticated_permissions['posts']['create']
        )
        native['update'] = bool(
            native['update'] and authenticated_permissions['posts']['update']
        )
        native['publish'] = bool(
            native['publish'] and authenticated_permissions['posts']['publish']
        )
        editorial = capabilities.get('editorial')
        if isinstance(editorial, Mapping):
            editorial = dict(editorial)
            editorial['read'] = bool(editorial.get('read') and native['read'])
            editorial['write'] = bool(editorial.get('write') and native['update'])
            if not editorial['write']:
                editorial['writable_fields'] = []
                editorial['conditionally_writable_fields'] = []
            capabilities['editorial'] = editorial
        capabilities.update({
            'authenticated': True,
            'authenticated_author': {
                'id': str(user['id']),
                'name': str(user.get('name', '')),
            },
            'authenticated_permissions': authenticated_permissions,
            'native': native,
            'permission_verification': 'Authenticated role checked; each target rechecked at write',
        })
        self._discovery['capabilities'] = capabilities
        return json_clone(capabilities)

    async def discover_authors(self) -> dict[str, Any]:
        """Return freshly checked, assignable users for WordPress posts.

        Author discovery is deliberately independent of ``validate_connection``
        and its cached capability document: the authenticated user and the
        complete user collection are both fetched on every call.
        """

        me_path = self._wp_path("wp/v2/users/me")
        try:
            user, _ = await self._json_request(
                "GET", me_path, params={"context": "edit"}
            )
        except ConnectorError as exc:
            if exc.status_code == 401:
                raise AuthenticationError(
                    "WordPress rejected author discovery authentication",
                    status_code=401,
                    method="GET",
                    url=exc.url,
                    response_received=True,
                ) from None
            raise ConnectorError(
                "WordPress could not verify author discovery permissions",
                status_code=exc.status_code,
                method="GET",
                url=exc.url,
                transport_error=exc.transport_error,
                response_received=exc.response_received,
            ) from None

        if not isinstance(user, Mapping):
            raise AuthenticationError("WordPress did not verify an authenticated user")
        user_id_value = user.get("id")
        if type(user_id_value) is not int or user_id_value < 1:
            raise AuthenticationError("WordPress did not verify an authenticated user")
        authenticated_user_id = str(user_id_value)

        user_capabilities = user.get("capabilities")
        required_connection_capabilities = (
            "edit_posts",
            "publish_posts",
            "edit_others_posts",
        )
        if not isinstance(user_capabilities, Mapping) or any(
            capability in user_capabilities and type(user_capabilities[capability]) is not bool
            for capability in required_connection_capabilities
        ):
            connection_capabilities_complete = False
        else:
            connection_capabilities_complete = True

        try:
            users = await self._fetch_collection(
                "users", params={"context": "edit"}
            )
        except IncompleteInventory:
            return {
                "items": [],
                "complete": False,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "authenticated_user_id": authenticated_user_id,
                "blockers": ["author_listing_incomplete"],
            }
        except ConnectorError as exc:
            if exc.status_code == 403:
                return {
                    "items": [],
                    "complete": False,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "authenticated_user_id": authenticated_user_id,
                    "blockers": ["author_listing_denied"],
                }
            if exc.status_code == 401:
                raise AuthenticationError(
                    "WordPress rejected author discovery authentication",
                    status_code=401,
                    method="GET",
                    url=exc.url,
                    response_received=True,
                ) from None
            raise ConnectorError(
                "WordPress author listing request failed",
                status_code=exc.status_code,
                method="GET",
                url=exc.url,
                transport_error=exc.transport_error,
                response_received=exc.response_received,
            ) from None

        blockers: list[str] = []
        if not connection_capabilities_complete:
            blockers.append("connection_capabilities_incomplete")

        candidates: list[dict[str, str]] = []
        malformed_records = False
        for record in users:
            record_id = record.get("id")
            name = record.get("name")
            capabilities = record.get("capabilities")
            if type(record_id) is not int or record_id < 1 or not isinstance(name, str):
                malformed_records = True
                continue
            if not isinstance(capabilities, Mapping) or type(
                capabilities.get("edit_posts", False)
            ) is not bool:
                malformed_records = True
                continue
            if capabilities.get("edit_posts") is True:
                candidates.append({"id": str(record_id), "name": name})

        if malformed_records:
            blockers.append("author_records_incomplete")
        if blockers:
            return {
                "items": [],
                "complete": False,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "authenticated_user_id": authenticated_user_id,
                "blockers": blockers,
            }

        if (
            user_capabilities.get("edit_posts") is not True
            or user_capabilities.get("publish_posts") is not True
        ):
            return {
                "items": [],
                "complete": True,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "authenticated_user_id": authenticated_user_id,
                "blockers": ["connection_cannot_publish"],
            }

        if user_capabilities.get("edit_others_posts") is True:
            assignable = candidates
            final_blockers: list[str] = []
        else:
            assignable = [
                candidate
                for candidate in candidates
                if candidate["id"] == authenticated_user_id
            ]
            final_blockers = []

        return {
            "items": assignable,
            "complete": True,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "authenticated_user_id": authenticated_user_id,
            "blockers": final_blockers,
            "warnings": [] if user_capabilities.get("edit_others_posts") is True else ["connection_can_only_assign_self"],
        }

    def _discover_status_names(self) -> list[str]:
        names: set[str] = set()
        for route in self._routes:
            match = re.search(r"/wp/v2/statuses/([^/]+)$", route.rstrip("/"))
            if match and "(?P" not in match.group(1):
                names.add(match.group(1))
        return sorted(names)

    async def _ensure_ready(
        self,
        *,
        write: bool = False,
        endpoint: str | None = None,
    ) -> dict[str, Any]:
        capabilities = await self.discover()
        if not capabilities.get("wp_v2"):
            raise ConnectorError("WordPress wp/v2 REST API is not available")
        if write:
            if endpoint == "pages":
                if not self._supports_item("/wp/v2/pages", "POST"):
                    raise ConnectorError(
                        "WordPress page item write capability is not available"
                    )
            elif not capabilities.get("native", {}).get("update"):
                raise ConnectorError("WordPress content write capability is not available")
        return capabilities

    async def _ensure_write_permission(
        self,
        capability: str,
        *,
        endpoint: str = "posts",
    ) -> None:
        """Recheck the authenticated user's permission at the mutation boundary."""

        # REST route discovery describes the public endpoint schema, not the
        # current user's authorization.  Revalidate immediately before every
        # mutation because a role can be changed after onboarding or while a
        # queued job is waiting.
        capabilities = await self.validate_connection()
        endpoint_permissions = capabilities.get("authenticated_permissions", {}).get(endpoint)
        if isinstance(endpoint_permissions, Mapping) and endpoint_permissions.get(capability) is True:
            return
        messages = {
            "create": "WordPress post creation capability is not available",
            "update": "WordPress content write capability is not available",
            "publish": "WordPress publication capability is not available",
        }
        resource = "page" if endpoint == "pages" else "post"
        message = messages.get(capability, "WordPress write capability is not available")
        if endpoint == "pages":
            message = message.replace("post", resource).replace("content", "page content")
        raise ConnectorError(message)

    def _resource_parts(self, resource_key: str) -> tuple[str, str, str]:
        if not isinstance(resource_key, str) or not resource_key.strip():
            raise ResourceNotFound("A resource key is required")
        key = resource_key.strip()
        cached = self._resource_routes.get(key)
        if cached:
            match = re.search(r"/wp/v2/([^/]+)/([0-9]+)$", cached.rstrip("/"))
            if match:
                return match.group(1), match.group(2), cached.rsplit("/", 1)[0]
        if key.startswith("http://") or key.startswith("https://"):
            for known_key, route in self._resource_routes.items():
                if known_key == key:
                    return self._resource_parts(known_key)
            raise ResourceNotFound("The URL is not a discovered WordPress resource")
        match = re.match(r"^([^:/]+)[:/]([0-9]+)$", key)
        if match:
            type_name, remote_id = match.groups()
        elif key.isdigit():
            type_name, remote_id = "post", key
        else:
            raise ResourceNotFound("WordPress resource keys must identify a numeric resource")
        normalized_type = type_name.lower().strip("/")
        descriptor = self._resource_type_by_key.get(normalized_type) or self._resource_type_by_base.get(
            normalized_type
        )
        endpoint = (
            descriptor["rest_base"]
            if descriptor is not None
            else _WP_CONTENT_TYPES.get(normalized_type, normalized_type)
        )
        if not re.fullmatch(r"[a-z0-9_-]+", endpoint):
            raise ResourceNotFound("The WordPress resource type is invalid")
        return endpoint, remote_id, f"/wp/v2/{endpoint}"

    def _resource_type(self, endpoint: str) -> str:
        descriptor = self._resource_type_by_base.get(endpoint)
        if descriptor is not None:
            return str(descriptor["key"])
        return {
            "posts": "post",
            "pages": "page",
            "media": "media",
            "users": "author",
        }.get(endpoint, endpoint.rstrip("s"))

    async def _fetch_collection(
        self,
        endpoint: str,
        *,
        params: Mapping[str, object] | None = None,
        max_pages: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._fetch_wp_collection(
            self._wp_path(f"wp/v2/{endpoint}"), params=params, max_pages=max_pages,
        )

    async def inventory(self, *, modified_after: str | None = None) -> list[dict[str, Any]]:
        """Return content inventory, optionally limited to recently changed items.

        WordPress post-type collections support ``modified_after``. Incremental
        polling deliberately omits users: author identities are useful during a
        full reconciliation but are not page changes and would otherwise cause
        every poll to download the complete user collection.
        """
        await self._ensure_ready()
        records: list[dict[str, Any]] = []
        endpoints: list[str] = ["posts"]
        if self._supports("/wp/v2/pages", "GET"):
            endpoints.append("pages")
        if modified_after is None and self._supports("/wp/v2/users", "GET"):
            # Author identities are editorial data used for attribution and
            # publication selection; inventory never writes user records.
            endpoints.append("users")
        for descriptor in self._resource_types:
            if not descriptor.get("inventoryable"):
                continue
            endpoint = str(descriptor["rest_base"])
            if endpoint not in endpoints:
                endpoints.append(endpoint)
        for endpoint in endpoints:
            params: dict[str, object] = {
                "context": "edit",
                "_embed": "author,wp:featuredmedia",
            }
            if modified_after is not None:
                params["modified_after"] = modified_after
            raw_items = await self._fetch_collection(
                endpoint,
                params=params,
            )
            for raw in raw_items:
                normalized = self._normalize(raw, endpoint)
                self._remember_route(normalized, endpoint)
                records.append(normalized)
        return json_clone(records)

    def _remember_route(self, record: Mapping[str, Any], endpoint: str) -> None:
        resource_key = str(record["resource_key"])
        self._resource_routes[resource_key] = f"/wp/v2/{endpoint}/{record['id']}"

    @staticmethod
    def _raw_meta(raw: Mapping[str, Any]) -> Mapping[str, Any]:
        value = raw.get("meta", {})
        return value if isinstance(value, Mapping) else {}

    def _seo_from_raw(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        meta = self._raw_meta(raw)
        result: dict[str, Any] = {}
        providers = ["forgeseo", "yoast", "rank_math"]
        for provider in providers:
            fields = _SEO_READ_KEYS[provider]
            values = {
                public_name: meta[meta_name]
                for public_name, meta_name in fields.items()
                if meta_name in meta and meta[meta_name] not in (None, "")
            }
            if values:
                result[provider] = copy_json(values)
        managed=raw.get('forgeseo_seo')
        if isinstance(managed,Mapping):
            result['forgeseo']={key:str(managed.get(key,'')) for key in ('title','description')}
        return result

    def _editorial_source(self, raw: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
        resource_type = self._resource_type(endpoint)
        if resource_type in {"post", "page"} or endpoint not in {"media", "users"}:
            source: dict[str, Any] = {
                "resource_type": resource_type,
                "slug": raw.get("slug"),
                "title": _field_text(raw.get("title")),
                "body": _field_text(raw.get("content")),
                "excerpt": _field_text(raw.get("excerpt")),
                "status": raw.get("status"),
                "author": raw.get("author"),
                "featured_media": raw.get("featured_media"),
                "categories": copy_json(raw.get("categories", [])),
                "tags": copy_json(raw.get("tags", [])),
            }
            seo = self._seo_from_raw(raw)
            if seo:
                source["seo"] = seo
            return source
        if endpoint == "media":
            return {
                "resource_type": "media",
                "slug": raw.get("slug"),
                "title": _field_text(raw.get("title")),
                "body": _field_text(raw.get("description")),
                "caption": _field_text(raw.get("caption")),
                "alt_text": raw.get("alt_text", ""),
                "status": raw.get("status"),
                "author": raw.get("author"),
                "post": raw.get("post"),
            }
        return {
            "resource_type": "author",
            "name": raw.get("name") or _field_text(raw.get("name")),
            "description": _field_text(raw.get("description")),
            "url": raw.get("link") or raw.get("url"),
        }

    def _metadata(self, raw: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "slug": raw.get("slug"),
            "excerpt": _field_text(raw.get("excerpt")),
            "author_id": raw.get("author"),
            "featured_media_id": raw.get("featured_media"),
            "status": raw.get("status"),
        }
        embedded = raw.get("_embedded")
        if isinstance(embedded, Mapping):
            # Preserve related author/media source data for audit and restore
            # snapshots without treating it as writable control data.
            if "author" in embedded:
                metadata["author"] = copy_json(embedded["author"])
            if "wp:featuredmedia" in embedded:
                metadata["media"] = copy_json(embedded["wp:featuredmedia"])
        if raw.get("categories") is not None:
            metadata["categories"] = copy_json(raw.get("categories"))
        if raw.get("tags") is not None:
            metadata["tags"] = copy_json(raw.get("tags"))
        if self._available_statuses:
            metadata["available_statuses"] = list(self._available_statuses)
        seo = self._seo_from_raw(raw)
        if seo:
            metadata["seo"] = seo
        if endpoint == "media":
            metadata.update(
                {
                    "alt_text": raw.get("alt_text", ""),
                    "caption": _field_text(raw.get("caption")),
                    "mime_type": raw.get("mime_type"),
                    "source_url": raw.get("source_url"),
                }
            )
        return json_clone(metadata)

    def _normalize(self, raw_value: Mapping[str, Any], endpoint: str | None = None) -> dict[str, Any]:
        raw = json_clone(raw_value)
        if endpoint is None:
            endpoint = str(raw.get("type") or "posts")
            endpoint = _WP_CONTENT_TYPES.get(endpoint, endpoint)
        remote_id = raw.get("id")
        if remote_id is None:
            raise ConnectorError("WordPress resource response did not include an id")
        resource_type = self._resource_type(endpoint)
        resource_key = f"{resource_type}:{remote_id}"
        if resource_type == "author":
            title = str(raw.get("name") or "")
            body = _field_text(raw.get("description"))
        elif resource_type == "media":
            title = _field_text(raw.get("title"))
            body = _field_text(raw.get("description"))
        else:
            title = _field_text(raw.get("title"))
            body = _field_text(raw.get("content"))
        source = self._editorial_source(raw, endpoint)
        record = {
            "resource_key": resource_key,
            "resource_type": resource_type,
            "id": remote_id,
            "url": raw.get("link") or raw.get("source_url") or raw.get("url"),
            "title": title,
            "body": body,
            "status": raw.get("status"),
            "metadata": self._metadata(raw, endpoint),
            "source_hash": stable_hash(source),
            "raw": raw,
        }
        return json_clone(record)

    async def read(self, resource_key: str) -> dict[str, Any]:
        await self._ensure_ready()
        endpoint, remote_id, collection = self._resource_parts(resource_key)
        params: dict[str, object] = {"context": "edit"}
        if endpoint not in {"users"}:
            params["_embed"] = "author,wp:featuredmedia"
        try:
            payload, _ = await self._json_request(
                "GET",
                self._wp_path(f"wp/v2/{endpoint}/{remote_id}"),
                params=params,
            )
        except ConnectorError as exc:
            if exc.status_code == 404:
                raise ResourceNotFound(f"WordPress resource {resource_key} was not found") from exc
            raise
        if not isinstance(payload, Mapping):
            raise ConnectorError("WordPress resource response was not an object")
        normalized = self._normalize(payload, endpoint)
        self._resource_routes[normalized["resource_key"]] = f"/wp/v2/{endpoint}/{remote_id}"
        return normalized

    @staticmethod
    def _normalized_key(key: Any) -> str:
        return str(key).lower().replace("-", "_")

    def _check_protected_nested(self, changes: Any) -> None:
        for path, key in walk_mapping_keys(changes):
            normalized = self._normalized_key(key)
            # ``metadata.seo`` is a public alias for the verified ForgeSEO
            # route. Leave only that outer wrapper for the shape- and
            # capability-aware validation below; nested commerce or arbitrary
            # metadata keys remain protected by this walk.
            if path == "metadata" and normalized == "metadata":
                continue
            if normalized in _FORBIDDEN_KEY_NORMALIZED:
                raise ProtectedField(path)

    @staticmethod
    def _ensure_string(field: str, value: Any, *, allow_empty: bool = True) -> str:
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise UnsupportedField(field, f"WordPress field {field} must be a string")
        if "\x00" in value:
            raise UnsupportedField(field, f"WordPress field {field} contains a NUL")
        return value

    @staticmethod
    def _ensure_id(field: str, value: Any) -> int:
        remote_id = safe_int(value)
        if remote_id is None or remote_id < 0:
            raise UnsupportedField(field, f"WordPress field {field} must be a non-negative id")
        return remote_id

    def _validate_seo(self, value: Any) -> dict[str, str]:
        if (
            not self._forgeseo_seo_route
            or not self._forgeseo_fields
            or not self._forgeseo_seo_methods
        ):
            raise UnsupportedField("seo", "SEO writes require the documented ForgeSEO namespaced plugin")
        if not isinstance(value, Mapping) or not value:
            raise UnsupportedField("seo", "SEO changes must be a non-empty mapping")
        result: dict[str, str] = {}
        for field, item in value.items():
            name = str(field)
            if name not in _SEO_FIELDS or name not in self._forgeseo_fields:
                raise UnsupportedField(f"seo.{name}")
            result[name] = self._ensure_string(f"seo.{name}", item)
            if name == "canonical_url" and result[name]:
                result[name] = validate_public_url(result[name], resolve_dns=False)
        return result

    def _builder_present(self, raw: Mapping[str, Any]) -> bool:
        for path, key in walk_mapping_keys(raw.get("meta", {})):
            normalized = self._normalized_key(key)
            if normalized in {
                "_elementor_data",
                "elementor_data",
                "_builder_data",
                "builder_data",
                "_et_pb_use_builder",
                "_bricks_page_content_2",
                "_fl_builder_data",
                "_wpb_vc_js_status",
            } or any(token in normalized for token in ("elementor", "divi", "bricks", "oxygen", "wpbakery")):
                return True
        return False

    def _validate_common_lists(self, field: str, value: Any) -> list[int]:
        if not isinstance(value, list):
            raise UnsupportedField(field, f"WordPress field {field} must be a list of ids")
        result = []
        for item in value:
            result.append(self._ensure_id(field, item))
        return result

    def _validate_changes(
        self,
        changes: Mapping[str, Any],
        current: Mapping[str, Any],
        endpoint: str,
        *,
        allow_publish: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str] | None]:
        if not isinstance(changes, Mapping):
            raise UnsupportedField("changes", "Changes must be a mapping")
        self._check_protected_nested(changes)
        if endpoint == "users":
            raise UnsupportedField("author", "WordPress authors are read-only through this connector")

        payload: dict[str, Any] = {}
        seo: dict[str, str] | None = None
        seen_native: set[str] = set()
        if endpoint == "media":
            allowed = {
                "title",
                "body",
                "description",
                "caption",
                "alt_text",
                "slug",
                "status",
                "author_id",
                "author",
                "post",
            }
        else:
            allowed = {
                "title",
                "body",
                "content",
                "excerpt",
                "slug",
                "status",
                "author_id",
                "author",
                "featured_media",
                "categories",
                "tags",
                "seo",
                "metadata",
            }
        for key, value in changes.items():
            name = str(key)
            if name not in allowed:
                raise UnsupportedField(name)
            if name in {"body", "content"}:
                if "content" in seen_native:
                    raise UnsupportedField(name, "Use only one of body or content")
                if self._builder_present(current.get("raw", {})):
                    raise ProtectedField(name, "Builder-managed content is protected")
                payload["content"] = self._ensure_string(name, value)
                seen_native.add("content")
            elif name == "description" and endpoint == "media":
                payload["description"] = self._ensure_string(name, value)
            elif name == "title":
                payload["title"] = self._ensure_string(name, value)
                seen_native.add("title")
            elif name == "excerpt":
                payload["excerpt"] = self._ensure_string(name, value)
            elif name == "caption":
                payload["caption"] = self._ensure_string(name, value)
            elif name == "alt_text":
                payload["alt_text"] = self._ensure_string(name, value)
            elif name == "slug":
                slug = self._ensure_string(name, value)
                if len(slug) > 200 or any(char.isspace() for char in slug):
                    raise UnsupportedField(name, "WordPress slugs must be compact and whitespace-free")
                payload["slug"] = slug
            elif name == "status":
                if not isinstance(value, str) or value not in _WP_STATUS_VALUES:
                    raise UnsupportedField(name, "WordPress status is not supported")
                if value == "publish" and not allow_publish:
                    raise UnsupportedField(name, "Use publish() for publication")
                if not allow_publish and value not in _WP_UPDATE_STATUS_VALUES:
                    raise UnsupportedField(name, "WordPress status is protected")
                payload["status"] = value
            elif name in {"author_id", "author"}:
                if "author" in seen_native:
                    raise UnsupportedField(name, "Use only one of author_id or author")
                payload["author"] = self._ensure_id(name, value)
                seen_native.add("author")
            elif name == "featured_media":
                payload[name] = self._ensure_id(name, value)
            elif name == "post" and endpoint == "media":
                payload[name] = self._ensure_id(name, value)
            elif name in {"categories", "tags"}:
                payload[name] = self._validate_common_lists(name, value)
            elif name == "seo":
                seo = self._validate_seo(value)
            elif name == "metadata":
                if not isinstance(value, Mapping) or set(value) != {"seo"}:
                    raise UnsupportedField(name, "Only documented SEO metadata is writable")
                seo = self._validate_seo(value["seo"])
        return payload, seo

    def _seo_route_path(self, endpoint: str, remote_id: str) -> str:
        route = self._forgeseo_seo_route
        if not route:
            raise UnsupportedField("seo")
        if self._seo_route_resource(route) != endpoint:
            raise UnsupportedField(
                "seo",
                "ForgeSEO SEO route is not a verified item resource route",
            )
        replaced = re.sub(r"\(\?P<id>[^)]+\)|\{id\}", remote_id, route)
        if re.search(r"\(\?P<[^>]+>", replaced):
            raise UnsupportedField("seo", "ForgeSEO SEO route contains an unsupported path parameter")
        return self._wp_path(replaced.lstrip("/"))

    def _operation_route_path(self, operation_key: str) -> str | None:
        route = self._forgeseo_operation_route
        if not route:
            return None
        encoded = quote(operation_key, safe="")
        replaced = re.sub(r"\(\?P<operation_key>[^)]+\)|\{operation_key\}", encoded, route)
        if re.search(r"\(\?P<[^>]+>", replaced):
            return None
        return self._wp_path(replaced.lstrip("/"))

    @staticmethod
    def _operation_headers(operation_key: str | None) -> dict[str, str]:
        """Mark a mutating request so the optional connector can suppress its own echo.

        The marker is request-scoped.  WordPress uses it only while processing
        this mutation, and the webhook endpoint treats it as a correlation
        hint, never as write authority.
        """

        if operation_key is None:
            return {}
        return {"X-ForgeSEO-Operation-Key": operation_key_header(operation_key)}

    @staticmethod
    def _snapshot_raw(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
        value: Any = snapshot.get("raw") if isinstance(snapshot, Mapping) and "raw" in snapshot else snapshot
        if not isinstance(value, Mapping):
            raise ConnectorError("A connector snapshot must contain a JSON object")
        return value

    def _snapshot_changes(self, snapshot: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
        raw = self._snapshot_raw(snapshot)
        if endpoint == "users":
            raise UnsupportedField("author", "WordPress authors cannot be restored")
        changes: dict[str, Any] = {}
        title = raw.get("title")
        if title is not None:
            changes["title"] = _field_text(title) if endpoint != "users" else str(raw.get("name", ""))
        if endpoint == "media":
            if "description" in raw:
                changes["description"] = _field_text(raw.get("description"))
            if "caption" in raw:
                changes["caption"] = _field_text(raw.get("caption"))
            if "alt_text" in raw:
                changes["alt_text"] = str(raw.get("alt_text") or "")
        else:
            if "content" in raw:
                changes["body"] = _field_text(raw.get("content"))
            if "excerpt" in raw:
                changes["excerpt"] = _field_text(raw.get("excerpt"))
            if "author" in raw and raw.get("author") is not None:
                changes["author_id"] = raw.get("author")
            if "featured_media" in raw and raw.get("featured_media") is not None:
                changes["featured_media"] = raw.get("featured_media")
            for field in ("categories", "tags"):
                if field in raw and isinstance(raw[field], list):
                    changes[field] = copy_json(raw[field])
        if raw.get("slug") is not None:
            changes["slug"] = str(raw["slug"])
        if raw.get("status") in _WP_STATUS_VALUES:
            changes["status"] = raw["status"]
        if isinstance(raw.get('forgeseo_seo'),Mapping):
            changes['seo']={key:str(raw['forgeseo_seo'].get(key,'')) for key in ('title','description')}
        return changes

    def _native_rollback_payload(
        self,
        current: Mapping[str, Any],
        native_payload: Mapping[str, Any],
        endpoint: str,
    ) -> dict[str, Any]:
        """Build a validated inverse for a native write before a second route."""

        snapshot_changes = self._snapshot_changes(current, endpoint)
        inverse_changes: dict[str, Any] = {}
        source_names = {"content": "body", "author": "author_id"}
        for field in native_payload:
            source_name = source_names.get(str(field), str(field))
            if source_name not in snapshot_changes:
                raise ConnectorError(
                    f"WordPress rollback could not capture native field {field}"
                )
            inverse_changes[source_name] = snapshot_changes[source_name]
        inverse_payload, _ = self._validate_changes(
            inverse_changes,
            current,
            endpoint,
            allow_publish=True,
        )
        if set(inverse_payload) != set(native_payload):
            raise ConnectorError("WordPress rollback did not cover the native write")
        return inverse_payload

    @staticmethod
    def _id_values(value: Any) -> list[int] | None:
        """Normalize WordPress id lists from raw or embedded REST shapes."""

        if not isinstance(value, list):
            return None
        result: list[int] = []
        for item in value:
            remote_id = safe_int(item.get("id")) if isinstance(item, Mapping) else safe_int(item)
            if remote_id is None:
                return None
            result.append(remote_id)
        return result

    def _native_payload_matches(
        self,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
        endpoint: str,
    ) -> bool:
        """Confirm that an authenticated read reflects every native field sent."""

        source = self._editorial_source(self._snapshot_raw(record), endpoint)
        source_names = {"content": "body", "description": "body"}
        text_fields = {
            "title",
            "body",
            "content",
            "description",
            "excerpt",
            "caption",
            "alt_text",
            "slug",
        }
        list_fields = {"categories", "tags"}
        for field, expected in payload.items():
            source_name = source_names.get(str(field), str(field))
            if source_name not in source:
                return False
            actual = source[source_name]
            if field in list_fields:
                if self._id_values(actual) != self._id_values(expected):
                    return False
            elif field in text_fields:
                if _field_text(actual) != _field_text(expected):
                    return False
            elif field in {"author", "featured_media", "post"}:
                if safe_int(actual) != safe_int(expected):
                    return False
            elif actual != expected:
                return False
        return True

    def _native_source_hash_excluding(
        self,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
        endpoint: str,
    ) -> str:
        """Fingerprint all native source fields except those intentionally written."""

        source = self._editorial_source(self._snapshot_raw(record), endpoint)
        source_names = {"content": "body", "description": "body"}
        for field in payload:
            source.pop(source_names.get(str(field), str(field)), None)
        return stable_hash(source)

    async def _recheck_source_before_write(
        self,
        resource_key: str,
        expected: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Refuse a mutation when the remote source changed after it was loaded.

        WordPress REST does not provide a reliable conditional-write primitive
        for every supported installation.  The authenticated read immediately
        before the request is therefore the last safe optimistic-concurrency
        check available to this connector.  Builder metadata is checked too:
        it is intentionally excluded from the editorial source hash, but a
        newly detected builder must still protect an upcoming body write.
        """

        observed = await self.read(resource_key)
        expected_hash = expected.get("source_hash")
        expected_builder = self._builder_present(self._snapshot_raw(expected))
        observed_builder = self._builder_present(self._snapshot_raw(observed))
        if (
            not isinstance(expected_hash, str)
            or observed.get("source_hash") != expected_hash
            or observed_builder != expected_builder
        ):
            raise SourceConflict(
                str(expected_hash),
                observed.get("source_hash"),
                resource_key=resource_key,
            )
        return observed

    async def _verify_native_write(
        self,
        resource_key: str,
        current: Mapping[str, Any],
        payload: Mapping[str, Any],
        endpoint: str,
        *,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        """Read back and validate a native mutation before reporting success."""

        try:
            after = await self.read(resource_key)
        except Exception as exc:
            raise AmbiguousOutcome(
                "Native WordPress write succeeded but its source could not be verified",
                operation_key=operation_key,
            ) from exc
        if not self._native_payload_matches(after, payload, endpoint):
            raise AmbiguousOutcome(
                "Native WordPress write succeeded but read-after-write verification did not match",
                operation_key=operation_key,
            )
        if self._native_source_hash_excluding(current, payload, endpoint) != self._native_source_hash_excluding(
            after,
            payload,
            endpoint,
        ):
            raise AmbiguousOutcome(
                "WordPress source changed outside the requested native fields",
                operation_key=operation_key,
            )
        return after

    async def _reconcile_native_timeout(
        self,
        resource_key: str,
        current: Mapping[str, Any],
        payload: Mapping[str, Any],
        endpoint: str,
        *,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile a native write whose response was lost without retrying.

        A transport failure does not prove that WordPress rejected a mutation.
        Read the authenticated source once and accept the outcome only when
        every requested field matches and every non-requested editorial field
        remains unchanged.  Any other observation is intentionally ambiguous:
        callers must reconcile or review it rather than sending a duplicate
        write.
        """

        try:
            after = await self.read(resource_key)
        except Exception as exc:
            raise AmbiguousOutcome(
                "Native WordPress update outcome is unknown and its source could not be reconciled",
                operation_key=operation_key,
            ) from exc
        if not self._native_payload_matches(after, payload, endpoint):
            raise AmbiguousOutcome(
                "Native WordPress update outcome is unknown and read-after-write verification did not match",
                operation_key=operation_key,
            )
        if self._native_source_hash_excluding(current, payload, endpoint) != self._native_source_hash_excluding(
            after,
            payload,
            endpoint,
        ):
            raise AmbiguousOutcome(
                "WordPress source changed outside the requested native fields while reconciling the update",
                operation_key=operation_key,
            )
        return after

    @staticmethod
    def _record_matches_create(record: Mapping[str, Any], article: Mapping[str, Any]) -> bool:
        expected_title = article.get("title")
        expected_body = article.get("body", article.get("content", ""))
        if expected_title is not None and str(record.get("title", "")) != str(expected_title):
            return False
        if expected_body is not None and str(record.get("body", "")) != str(expected_body):
            return False
        return True

    async def _reconcile_create(
        self,
        article: Mapping[str, Any],
        operation_key: str,
        slug: str,
    ) -> dict[str, Any]:
        # A plugin operation mapping is stronger than a slug lookup, but only
        # use it when the route was explicitly advertised by ForgeSEO.
        operation_path = self._operation_route_path(operation_key)
        if operation_path:
            try:
                payload, _ = await self._json_request("GET", operation_path)
                if isinstance(payload, Mapping) and payload.get("id") is not None:
                    mapped_type = str(payload.get("resource_type") or "post")
                    mapped_key = f"{mapped_type}:{payload['id']}"
                    record = await self.read(mapped_key)
                    if self._record_matches_create(record, article):
                        return record
            except ConnectorError:
                # A mapping endpoint may be permission-scoped; slug
                # reconciliation remains safe and deterministic.
                pass

        try:
            payload, _ = await self._json_request(
                "GET",
                self._wp_path("wp/v2/posts"),
                params={
                    "slug": slug,
                    "context": "edit",
                    "status": "any",
                    "_embed": "author,wp:featuredmedia",
                    "per_page": 10,
                },
            )
        except ConnectorError as exc:
            raise AmbiguousOutcome(
                "The draft request outcome is unknown and reconciliation failed",
                operation_key=operation_key,
            ) from exc
        if not isinstance(payload, list):
            raise AmbiguousOutcome(
                "The draft request outcome is unknown and reconciliation was inconclusive",
                operation_key=operation_key,
            )
        candidates: list[dict[str, Any]] = []
        for raw in payload:
            if not isinstance(raw, Mapping):
                continue
            record = self._normalize(raw, "posts")
            if raw.get("slug") == slug and self._record_matches_create(record, article):
                candidates.append(record)
        if len(candidates) == 1:
            record = candidates[0]
            self._remember_route(record, "posts")
            result = json_clone(record)
            result["outcome"] = "reconciled"
            return result
        raise AmbiguousOutcome(
            "The draft request outcome is unknown; no unique matching draft exists",
            operation_key=operation_key,
            candidates=[
                {
                    "resource_key": candidate.get("resource_key"),
                    "source_hash": candidate.get("source_hash"),
                }
                for candidate in candidates
            ],
        )

    def _article_payload(self, article: Mapping[str, Any], operation_key: str) -> tuple[dict[str, Any], str]:
        if not isinstance(article, Mapping):
            raise UnsupportedField("article", "Article must be a mapping")
        title = self._ensure_string("title", article.get("title", ""), allow_empty=False)
        body_value = article.get("body", article.get("content", ""))
        body = self._ensure_string("body", body_value)
        requested_slug = article.get("slug")
        if requested_slug is not None:
            slug = self._ensure_string("slug", requested_slug, allow_empty=False)
            if len(slug) > 200 or any(char.isspace() for char in slug):
                raise UnsupportedField("slug", "WordPress slugs must be compact and whitespace-free")
        else:
            digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:12]
            slug = f"{slugify(title)}--forge-{digest}"[:200]
        payload: dict[str, Any] = {"title": title, "content": body, "status": "draft", "slug": slug}
        allowed = {"title", "body", "content", "slug", "excerpt", "author_id", "author", "featured_media", "categories", "tags"}
        self._check_protected_nested(article)
        for key in article:
            if str(key) not in allowed and key not in {"brief", "sources", "status", "metadata"}:
                raise UnsupportedField(str(key))
        if "excerpt" in article:
            payload["excerpt"] = self._ensure_string("excerpt", article["excerpt"])
        for source_name, api_name in (("author_id", "author"), ("featured_media", "featured_media")):
            if source_name in article:
                payload[api_name] = self._ensure_id(source_name, article[source_name])
        if "author" in article:
            if "author" in payload:
                raise UnsupportedField("author", "Use only one of author_id or author")
            payload["author"] = self._ensure_id("author", article["author"])
        for field in ("categories", "tags"):
            if field in article:
                payload[field] = self._validate_common_lists(field, article[field])
        return payload, slug

    async def reconcile_draft(self, article: dict[str, Any], operation_key: str) -> dict[str, Any]:
        """Read-only recovery after a process died during creation; never POST again."""
        await self._ensure_ready()
        operation_key = operation_key_header(operation_key)
        _, slug = self._article_payload(article, operation_key)
        return await self._reconcile_create(article, operation_key, slug)

    def matches_snapshot(self, current: Mapping[str, Any], snapshot: Mapping[str, Any], *, ignore_status: bool = False) -> bool:
        """Compare managed source fields, excluding volatile render/date hints."""
        endpoint, _, _ = self._resource_parts(str(current['resource_key']))
        left = self._editorial_source(self._snapshot_raw(current), endpoint)
        right = self._editorial_source(self._snapshot_raw(snapshot), endpoint)
        if ignore_status:
            left.pop('status', None)
            right.pop('status', None)
        return left == right

    async def create_draft(self, article: dict[str, Any], operation_key: str) -> dict[str, Any]:
        capabilities = await self._ensure_ready(write=True)
        if not capabilities.get("native", {}).get("create"):
            raise ConnectorError("WordPress post creation capability is not available")
        operation_key = operation_key_header(operation_key)
        payload, slug = self._article_payload(article, operation_key)
        await self._ensure_write_permission("create")
        try:
            created, _ = await self._json_request(
                "POST",
                self._wp_path("wp/v2/posts"),
                json=payload,
                headers={"X-ForgeSEO-Operation-Key": operation_key},
            )
        except ConnectorError as exc:
            if not exc.transport_error:
                raise
            return await self._reconcile_create(article, operation_key, slug)
        if isinstance(created, Mapping) and created.get("id") is not None:
            record = self._normalize(created, "posts")
            self._remember_route(record, "posts")
            return record
        # A successful response without a usable id is treated like a lost
        # response; the deterministic lookup avoids a second POST.
        return await self._reconcile_create(article, operation_key, slug)

    async def _write_native(
        self,
        endpoint: str,
        remote_id: str,
        payload: Mapping[str, Any],
        *,
        operation_key: str | None = None,
    ) -> tuple[Any, httpx.Response]:
        capability = "publish" if endpoint in {"posts", "pages"} and payload.get("status") == "publish" else "update"
        await self._ensure_write_permission(capability, endpoint=endpoint)
        return await self._json_request(
            "POST",
            self._wp_path(f"wp/v2/{endpoint}/{remote_id}"),
            json=dict(payload),
            headers=self._operation_headers(operation_key),
        )

    async def _update_loaded(
        self,
        resource_key: str,
        current: Mapping[str, Any],
        changes: Mapping[str, Any],
        *,
        allow_publish: bool = False,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        endpoint, remote_id, _ = self._resource_parts(resource_key)
        self._ensure_item_write_capability(endpoint)
        payload, seo = self._validate_changes(changes, current, endpoint, allow_publish=allow_publish)
        if not payload and not seo:
            return json_clone(current)
        current = await self._recheck_source_before_write(resource_key, current)
        # Revalidate against the just-observed source so a builder that appeared
        # between the initial read and this write is protected as well.
        payload, seo = self._validate_changes(changes, current, endpoint, allow_publish=allow_publish)
        if not payload and not seo:
            return json_clone(current)
        native_after: dict[str, Any] | None = None
        if payload:
            try:
                await self._write_native(
                    endpoint,
                    remote_id,
                    payload,
                    operation_key=operation_key,
                )
            except ConnectorError as exc:
                if not exc.transport_error:
                    raise
                native_after = await self._reconcile_native_timeout(
                    resource_key,
                    current,
                    payload,
                    endpoint,
                    operation_key=operation_key,
                )
            else:
                native_after = await self._verify_native_write(
                    resource_key,
                    current,
                    payload,
                    endpoint,
                    operation_key=operation_key,
                )
        if seo:
            try:
                if endpoint not in {"posts", "pages"}:
                    raise UnsupportedField("seo", "SEO writes are supported only for posts and pages")
                write_base = native_after if native_after is not None else current
                await self._recheck_source_before_write(resource_key, write_base)
                await self._ensure_write_permission("update", endpoint=endpoint)
                method = next(
                    (
                        candidate
                        for candidate in ("POST", "PUT", "PATCH")
                        if candidate in self._forgeseo_seo_methods
                    ),
                    None,
                )
                if method is None:
                    raise UnsupportedField(
                        "seo",
                        "ForgeSEO SEO route does not advertise a supported write method",
                    )
                await self._json_request(
                    method,
                    self._seo_route_path(endpoint, remote_id),
                    json=seo,
                    headers=self._operation_headers(operation_key),
                )
            except Exception as seo_error:
                if not payload or native_after is None:
                    raise
                try:
                    observed = await self.read(resource_key)
                    if observed.get("source_hash") != native_after.get("source_hash"):
                        raise ConnectorError(
                            "WordPress SEO write outcome changed the native source"
                        )
                    inverse_payload = self._native_rollback_payload(
                        current,
                        payload,
                        endpoint,
                    )
                    await self._recheck_source_before_write(resource_key, native_after)
                    await self._write_native(
                        endpoint,
                        remote_id,
                        inverse_payload,
                        operation_key=operation_key,
                    )
                    restored = await self.read(resource_key)
                    if restored.get("source_hash") != current.get("source_hash"):
                        raise ConnectorError("WordPress native compensation did not restore the source")
                except Exception as compensation_error:
                    raise AmbiguousOutcome(
                        "WordPress mutation may be partially applied and needs reconciliation",
                        operation_key=operation_key,
                    ) from compensation_error
                raise seo_error
        if native_after is not None and not seo:
            return native_after
        # Plugin fields are deliberately verified by an authenticated read so
        # the returned source hash reflects what the site actually stored.
        return await self.read(resource_key)

    async def update(
        self,
        resource_key: str,
        changes: dict[str, Any],
        expected_hash: str,
        *,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_ready()
        endpoint, _, _ = self._resource_parts(resource_key)
        await self._ensure_ready(write=True, endpoint=endpoint)
        if not isinstance(expected_hash, str) or not expected_hash:
            raise SourceConflict(str(expected_hash), None, resource_key=resource_key)
        current = await self.read(resource_key)
        if current.get("source_hash") != expected_hash:
            raise SourceConflict(expected_hash, current.get("source_hash"), resource_key=resource_key)
        return await self._update_loaded(
            resource_key,
            current,
            changes,
            operation_key=operation_key,
        )

    async def publish(
        self,
        remote_id: str,
        expected_hash: str | None = None,
        *,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_ready()
        endpoint, resource_id, _ = self._resource_parts(str(remote_id))
        await self._ensure_ready(write=True, endpoint=endpoint)
        if endpoint not in {"posts", "pages"}:
            raise UnsupportedField("status", "Only posts and pages can be published")
        self._ensure_item_write_capability(endpoint)
        key = f"{self._resource_type(endpoint)}:{resource_id}"
        # Always take a source snapshot immediately before publication.  When
        # no caller hash is supplied it still protects the post-write
        # verification from silently accepting a concurrent content edit.
        expected_snapshot: dict[str, Any] = await self.read(key)
        if expected_hash is not None:
            if expected_snapshot['source_hash'] != expected_hash:
                raise SourceConflict(expected_hash, expected_snapshot['source_hash'], resource_key=key)
        try:
            _, _ = await self._write_native(
                endpoint,
                resource_id,
                {"status": "publish"},
                operation_key=operation_key,
            )
        except ConnectorError as exc:
            if not exc.transport_error:
                raise
            try:
                current = await self.read(key)
            except ConnectorError as read_exc:
                raise AmbiguousOutcome(
                    "Publication outcome is unknown",
                    operation_key=operation_key,
                ) from read_exc
            if expected_snapshot is not None and not self.matches_snapshot(
                current,
                expected_snapshot,
                ignore_status=True,
            ):
                raise SourceConflict(
                    expected_hash or expected_snapshot["source_hash"],
                    current.get("source_hash"),
                    resource_key=key,
                )
            if current.get("status") == "publish":
                return current
            raise AmbiguousOutcome(
                "Publication outcome is unknown",
                operation_key=operation_key,
            )
        try:
            current = await self.read(key)
        except Exception as exc:
            raise AmbiguousOutcome(
                "Publication succeeded but its result could not be verified",
                operation_key=operation_key,
            ) from exc
        if expected_snapshot is not None and not self.matches_snapshot(
            current,
            expected_snapshot,
            ignore_status=True,
        ):
            raise SourceConflict(
                expected_hash or expected_snapshot["source_hash"],
                current.get("source_hash"),
                resource_key=key,
            )
        if current.get("status") != "publish":
            raise AmbiguousOutcome(
                "Publication succeeded but read-after-write verification did not confirm publish",
                operation_key=operation_key,
            )
        return current

    async def restore(
        self,
        resource_key: str,
        snapshot: dict[str, Any],
        expected_hash: str | None = None,
        *,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_ready()
        endpoint, _, _ = self._resource_parts(resource_key)
        await self._ensure_ready(write=True, endpoint=endpoint)
        current = await self.read(resource_key)
        if expected_hash is not None and current.get("source_hash") != expected_hash:
            raise SourceConflict(expected_hash, current.get("source_hash"), resource_key=resource_key)
        changes = self._snapshot_changes(snapshot, endpoint)
        # Builder-managed bodies remain protected.  A rollback of a newly
        # published temporary post may still need to restore its status,
        # though, and replaying an unchanged body would incorrectly trigger
        # that protection.  Only omit the body when the current content is
        # exactly the snapshot content; changed builder content must continue
        # to fail through _validate_changes below.
        if (
            "body" in changes
            and self._builder_present(current.get("raw", {}))
            and current.get("body") == changes["body"]
        ):
            changes.pop("body")
        # Even an unguarded restore gets an immediate current-source
        # precondition between its read and write path.
        return await self._update_loaded(
            resource_key,
            current,
            changes,
            allow_publish=True,
            operation_key=operation_key,
        )


__all__ = ["WordPressClient"]
