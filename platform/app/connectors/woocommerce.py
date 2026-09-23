"""Conservative async WooCommerce REST API client.

WooCommerce records contain both editorial fields and operational commerce
state.  This client only writes the former and never sends an entire snapshot
back to the store, which prevents price, stock, SKU, tax, variation, and
builder data drift during restore.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from .base import AsyncConnector
from .common import (
    copy_json,
    is_numeric_id_placeholder,
    json_clone,
    safe_int,
    stable_hash,
    text_value,
    walk_mapping_keys,
)
from .errors import (
    AmbiguousOutcome,
    ConnectorError,
    ProtectedField,
    ResourceNotFound,
    SourceConflict,
    UnsupportedField,
)


_WOO_PROTECTED_FIELDS = {
    "price",
    "regular_price",
    "sale_price",
    "date_on_sale_from",
    "date_on_sale_to",
    "stock",
    "stock_quantity",
    "stock_status",
    "manage_stock",
    "backorders",
    "low_stock_amount",
    "sold_individually",
    "sku",
    "global_unique_id",
    "weight",
    "dimensions",
    "shipping",
    "shipping_class",
    "tax_status",
    "tax_class",
    "virtual",
    "downloadable",
    "downloads",
    "download_limit",
    "download_expiry",
    "purchase_note",
    "attributes",
    "default_attributes",
    "variations",
    "grouped_products",
    "upsell_ids",
    "cross_sell_ids",
    "featured",
    "catalog_visibility",
    "on_sale",
    "price_html",
    "average_rating",
    "rating_count",
    "related_ids",
    "reviews_allowed",
    "meta_data",
    "meta",
    "metadata",
    "custom_fields",
    "customfields",
    "store_data",
    "order",
    "orders",
    "order_id",
    "payment",
    "payments",
    "payment_method",
    "payment_method_title",
    "transaction_id",
    "customer",
    "customers",
    "customer_id",
    "billing",
    "builder",
    "builder_data",
    "elementor",
    "elementor_data",
    "divi",
    "bricks",
    "acf",
}
_WOO_PROTECTED_NORMALIZED = {field.lower().replace("-", "_") for field in _WOO_PROTECTED_FIELDS}
_WOO_PROTECTED_RESOURCES = ("orders", "payments", "customers")
_WOO_EDITORIAL_FIELDS = {
    "product": ("name", "description", "short_description", "categories", "tags"),
    "category": ("name", "description", "slug"),
}

# These are deliberately limited to well-known builder markers.  A normal
# WooCommerce ``meta_data`` field is not itself evidence that a product is
# builder-managed; only a registered marker or an explicit builder name is.
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
    "bricks": {"_bricks_page_content", "_bricks_page_content_2"},
    "beaver_builder": {"_fl_builder_data", "_fl_builder_enabled"},
    "wpbakery": {"_wpb_vc_js_status", "_wpb_vc_js_interface"},
    "oxygen": {"_ct_builder_shortcodes", "ct_builder_shortcodes"},
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


def _field_text(value: Any) -> str:
    return text_value(value)


def _route_methods(route_info: Mapping[str, Any]) -> set[str]:
    methods: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str):
            methods.add(value.upper())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            methods.update(str(method).upper() for method in value)

    add(route_info.get("methods"))
    endpoints = route_info.get("endpoints")
    if isinstance(endpoints, Sequence) and not isinstance(endpoints, (str, bytes)):
        for endpoint in endpoints:
            if isinstance(endpoint, Mapping):
                add(endpoint.get("methods"))
    return methods


def _builder_provider_for_field(field: str) -> str | None:
    normalized = field.lower().replace("-", "_")
    for provider, fields in _BUILDER_METADATA_FIELDS.items():
        if normalized in {item.lower().replace("-", "_") for item in fields}:
            return provider
    for provider, prefixes in _BUILDER_FIELD_PREFIXES.items():
        if any(normalized.startswith(prefix) for prefix in prefixes):
            return provider
    return None


class WooCommerceClient(AsyncConnector):
    """Async WooCommerce v3 client with editorial-only writes."""

    def __init__(
        self,
        origin: str,
        credentials: dict[str, object],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(origin, credentials, transport, auth_kind="woocommerce")
        self._api_root = f"{self.origin.rstrip('/')}/wp-json/wc/v3"
        self._wp_root = f"{self.origin.rstrip('/')}/wp-json"
        self._discovery: dict[str, Any] | None = None
        self._routes: dict[str, Mapping[str, Any]] = {}
        self._resource_routes: dict[str, str] = {}
        self._seo_route: str | None = None
        self._seo_route_methods: set[str] = set()
        self._category_seo_route: str | None = None
        self._category_seo_route_methods: set[str] = set()

    def _woo_path(self, path: str = "") -> str:
        return f"{self._api_root}/{path.lstrip('/')}"

    def _route(self, endpoint: str) -> Mapping[str, Any] | None:
        target = "/" + endpoint.strip("/")
        candidates = {
            target.rstrip("/"),
            f"/wc/v3{target}".rstrip("/"),
            f"/wp-json/wc/v3{target}".rstrip("/"),
        }
        for route, info in self._routes.items():
            route_clean = str(route).rstrip("/")
            if route_clean in candidates:
                return info
        return None

    def _item_route(self, endpoint: str) -> Mapping[str, Any] | None:
        """Return the documented numeric child route for an endpoint.

        The REST index contains both collection routes and regex item routes.
        Treating any child route as an item route would accidentally promote
        ``/batch`` or another extension to update authority, so only a single
        named/brace placeholder is accepted here.
        """

        target = "/" + endpoint.strip("/")
        pattern = re.compile(
            rf"^(?:/wc/v3|/wp-json/wc/v3)?{re.escape(target)}"
            r"/(?P<placeholder>\(\?P<[^>]+>[^)]+\)|\{[^}]+\})/?$"
        )
        for route, info in self._routes.items():
            match = pattern.fullmatch(str(route).rstrip("/"))
            if match and is_numeric_id_placeholder(match.group("placeholder")):
                return info
        return None

    def _find_product_seo_route(self) -> tuple[str, Mapping[str, Any]] | None:
        """Find the optional, product-only ForgeSEO metadata route.

        The route is intentionally matched narrowly.  A generic ``/seo`` or
        arbitrary custom endpoint must not be promoted into write authority.
        Both WordPress REST index spellings (with and without ``/wp-json``)
        and named/brace numeric parameters are accepted.
        """

        pattern = re.compile(
            r"^(?:/wp-json)?/forgeseo/v1/products/"
            r"(?:\(\?P<[^>]+>[^)]+\)|\{[^}]+\})/seo/?$",
            re.IGNORECASE,
        )
        for route, info in self._routes.items():
            if (
                pattern.fullmatch(str(route).rstrip("/"))
                and self._seo_route_resource(route) == "products"
            ):
                return str(route), info
        return None

    def _find_product_category_seo_route(self) -> tuple[str, Mapping[str, Any]] | None:
        pattern = re.compile(
            r"^(?:/wp-json)?/forgeseo/v1/product-categories/"
            r"(?:\(\?P<[^>]+>[^)]+\)|\{[^}]+\})/seo/?$",
            re.IGNORECASE,
        )
        for route, info in self._routes.items():
            if (
                pattern.fullmatch(str(route).rstrip("/"))
                and self._seo_route_resource(route) == "product-categories"
            ):
                return str(route), info
        return None

    @staticmethod
    def _seo_route_resource(route: str) -> str | None:
        """Return the resource only for a verified numeric item SEO route.

        REST route metadata is remote input.  A matching path is not enough to
        establish that substituting a numeric WooCommerce ID is safe: a route
        such as ``(?P<slug>[^/]+)`` can be item-scoped while using a different
        identifier.  Only the canonical numeric forms used by the connector,
        plus the explicit ``{id}`` form, can grant SEO capability.
        """

        normalized = "/" + str(route).strip("/")
        match = re.fullmatch(
            r"/(?:wp-json/)?forgeseo/v1/"
            r"(?P<resource>products|product-categories)/"
            r"(?P<placeholder>\(\?P<id>[^)]+\)|\{id\})/seo/?",
            normalized,
            re.IGNORECASE,
        )
        if match is None:
            return None

        if not is_numeric_id_placeholder(match.group("placeholder")):
            return None
        return match.group("resource").lower()

    def _product_seo_path(self, remote_id: str) -> str:
        return self._seo_path("products", remote_id)

    def _seo_path(self, endpoint: str, remote_id: str) -> str:
        route = self._category_seo_route if endpoint == "products/categories" else self._seo_route
        if not route:
            raise UnsupportedField("seo", "SEO metadata requires the verified ForgeSEO connector")
        replaced = re.sub(r"\(\?P<[^>]+>[^)]+\)|\{[^}]+\}", str(remote_id), route)
        if re.search(r"\(\?P<[^>]+>|\{[^}]+\}", replaced):
            raise UnsupportedField("seo", "ForgeSEO product SEO route contains an unsupported path parameter")
        normalized = replaced.lstrip("/")
        if normalized.startswith("wp-json/"):
            return f"{self.origin.rstrip('/')}/{normalized}"
        return f"{self._wp_root}/{normalized}"

    def _supports(self, endpoint: str, method: str | None = None, *, item: bool = False) -> bool:
        info = self._item_route(endpoint) if item else self._route(endpoint)
        if info is None:
            return False
        if method is None:
            return True
        return method.upper() in _route_methods(info)

    def _builder_constraints(self, endpoint: str) -> dict[str, Any]:
        """Describe builder metadata exposed by this WooCommerce resource."""

        fields_by_provider: dict[str, set[str]] = {}

        def visit(value: Any, *, metadata_context: bool = False) -> None:
            if isinstance(value, Mapping):
                for raw_key, child in value.items():
                    field = str(raw_key)
                    normalized = field.lower().replace("-", "_")
                    provider = _builder_provider_for_field(field)
                    generic = normalized in _GENERIC_BUILDER_METADATA_FIELDS
                    if provider is not None or (metadata_context and generic):
                        fields_by_provider.setdefault(provider or "generic_builder", set()).add(field)
                    child_is_metadata = metadata_context or normalized in {
                        "meta",
                        "metadata",
                        "meta_data",
                        "custom_fields",
                        "customfields",
                    }
                    visit(child, metadata_context=child_is_metadata)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for child in value:
                    visit(child, metadata_context=metadata_context)

        route_info = self._route(endpoint)
        if route_info is not None:
            visit(route_info)
        item_info = self._item_route(endpoint)
        if item_info is not None:
            visit(item_info)

        metadata_fields = sorted(
            field for fields in fields_by_provider.values() for field in fields
        )
        detected = bool(metadata_fields)
        protected_content = ["description", "short_description"] if detected else []
        return {
            "detected": detected,
            "providers": sorted(fields_by_provider),
            "metadata_fields": metadata_fields,
            "detection_source": "woocommerce_rest_route_metadata",
            "metadata_write": False,
            "content_write": {
                "mode": "protected_when_builder_metadata_present" if detected else "native",
                "allowed_for_unmanaged_target": True,
                "blocked_for_builder_target": detected,
                "requires_target_check": detected,
            },
            "protected_fields": protected_content,
        }

    async def discover(self) -> dict[str, Any]:
        if self._discovery is not None:
            return json_clone(self._discovery["capabilities"])
        payload: Any
        try:
            payload, _ = await self._json_request("GET", f"{self._wp_root}/",authenticate=False)
        except ConnectorError as exc:
            if exc.status_code != 404:
                raise
            # A Woo-only reverse proxy may expose the namespace root but not
            # the general WP index.  It is still safe to inspect that root.
            payload, _ = await self._json_request("GET", f"{self._api_root}/", accepted_statuses=(200, 404))
            if payload is None:
                payload = {}
        if not isinstance(payload, Mapping):
            raise ConnectorError("WooCommerce API discovery returned an invalid document")
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
        seo_route = self._find_product_seo_route()
        self._seo_route = seo_route[0] if seo_route is not None else None
        self._seo_route_methods = _route_methods(seo_route[1]) if seo_route is not None else set()
        category_seo_route = self._find_product_category_seo_route()
        self._category_seo_route = category_seo_route[0] if category_seo_route is not None else None
        self._category_seo_route_methods = (
            _route_methods(category_seo_route[1]) if category_seo_route is not None else set()
        )
        has_namespace = "wc/v3" in namespaces or any(
            str(route).rstrip("/") in {"/wc/v3", "/wp-json/wc/v3"}
            or str(route).startswith(("/wc/v3/", "/wp-json/wc/v3/"))
            for route in self._routes
        )
        self._discovery = {
            "name": payload.get("name") if isinstance(payload.get("name"), str) else None,
            "namespaces": sorted(namespaces),
            "woocommerce_api": has_namespace,
            "routes": copy_json(self._routes),
        }
        product_collection_read = has_namespace and self._supports("products", "GET")
        product_item_read = has_namespace and self._supports("products", "GET", item=True)
        product_item_update = has_namespace and self._supports("products", "PUT", item=True)
        category_collection_read = has_namespace and self._supports("products/categories", "GET")
        category_item_read = has_namespace and self._supports(
            "products/categories", "GET", item=True
        )
        category_item_update = has_namespace and self._supports(
            "products/categories", "PUT", item=True
        )
        product_builder_constraints = self._builder_constraints("products")
        category_builder_constraints = self._builder_constraints("products/categories")
        product_seo_read = bool(self._seo_route and "GET" in self._seo_route_methods)
        category_seo_read = bool(
            self._category_seo_route and "GET" in self._category_seo_route_methods
        )
        product_seo_write = bool(
            product_seo_read
            and self._seo_route_methods.intersection({"POST", "PUT", "PATCH"})
        )
        category_seo_write = bool(
            category_seo_read
            and self._category_seo_route_methods.intersection({"POST", "PUT", "PATCH"})
        )
        seo_capability: dict[str, Any] = {
            "detected": self._seo_route is not None or self._category_seo_route is not None,
            "read": product_seo_read or category_seo_read,
            "write": product_seo_write or category_seo_write,
            "provider": "forgeseo"
            if self._seo_route is not None or self._category_seo_route is not None
            else None,
            "writable_fields": ["title", "description"]
            if product_seo_write or category_seo_write
            else [],
            "resource_types": [
                resource_type
                for resource_type, supported in (
                    ("product", product_seo_read),
                    ("category", category_seo_read),
                )
                if supported
            ],
            "route": self._seo_route,
        }
        if self._category_seo_route is not None:
            seo_capability["category_route"] = self._category_seo_route

        def resource_capabilities(
            *,
            collection_read: bool,
            item_read: bool,
            item_update: bool,
            resource_type: str,
            builder_constraints: dict[str, Any],
        ) -> dict[str, Any]:
            editorial_fields = list(_WOO_EDITORIAL_FIELDS[resource_type])
            protected_fields = sorted(
                set(_WOO_PROTECTED_FIELDS).union(builder_constraints["protected_fields"])
            )
            return {
                # Explicit route-level contract.  The flat aliases below are
                # retained for existing callers and mean the same thing.
                "collection": {"read": collection_read},
                "item": {"read": item_read, "update": item_update},
                "read": collection_read,
                "update": item_update,
                "editorial_fields": editorial_fields,
                "protected_fields": protected_fields,
                "builder_constraints": builder_constraints,
            }

        product_capabilities = resource_capabilities(
            collection_read=product_collection_read,
            item_read=product_item_read,
            item_update=product_item_update,
            resource_type="product",
            builder_constraints=product_builder_constraints,
        )
        category_capabilities = resource_capabilities(
            collection_read=category_collection_read,
            item_read=category_item_read,
            item_update=category_item_update,
            resource_type="category",
            builder_constraints=category_builder_constraints,
        )

        self._discovery["capabilities"] = {
            "kind": "woocommerce",
            "rest_api": True,
            "woocommerce_api": has_namespace,
            "products": product_capabilities,
            "categories": category_capabilities,
            "seo": seo_capability,
            "supported_editorial_fields": {
                resource_type: list(fields)
                for resource_type, fields in _WOO_EDITORIAL_FIELDS.items()
            },
            "protected_commerce_fields": sorted(_WOO_PROTECTED_FIELDS),
            "protected_commerce_resources": list(_WOO_PROTECTED_RESOURCES),
            "editorial": {
                "product": {
                    "read": product_item_read,
                    "write": product_item_update,
                    "writable_fields": list(_WOO_EDITORIAL_FIELDS["product"])
                    if product_item_update
                    else [],
                    "protected_fields": product_capabilities["protected_fields"],
                    "builder_constraints": product_builder_constraints,
                },
                "category": {
                    "read": category_item_read,
                    "write": category_item_update,
                    "writable_fields": list(_WOO_EDITORIAL_FIELDS["category"])
                    if category_item_update
                    else [],
                    "protected_fields": category_capabilities["protected_fields"],
                    "builder_constraints": category_builder_constraints,
                },
            },
        }
        return json_clone(self._discovery["capabilities"])

    async def validate_connection(self) -> dict[str, Any]:
        capabilities = await self.discover()
        if not capabilities.get("products", {}).get("collection", {}).get("read"):
            raise ConnectorError("WooCommerce product collection read capability is not available")
        data, _ = await self._json_request(
            "GET",
            self._woo_path("products"),
            params={"per_page": 1, "context": "edit"},
        )
        if not isinstance(data, list):
            raise ConnectorError("WooCommerce did not verify authenticated catalog access")
        if self._seo_route and "GET" in self._seo_route_methods:
            for item in data:
                if isinstance(item, Mapping) and item.get("id") is not None:
                    await self._read_resource_seo("products", str(item["id"]))
                    break
        if self._category_seo_route and "GET" in self._category_seo_route_methods:
            category_data, _ = await self._json_request(
                "GET",
                self._woo_path("products/categories"),
                params={"per_page": 1, "context": "edit"},
            )
            if isinstance(category_data, list):
                for item in category_data:
                    if isinstance(item, Mapping) and item.get("id") is not None:
                        await self._read_resource_seo("products/categories", str(item["id"]))
                        break
        return {
            **capabilities,
            "authenticated": True,
            "permission_verification": "Catalog read verified; write permissions checked per operation",
        }

    async def _ensure_ready(self, *, write: bool = False) -> dict[str, Any]:
        capabilities = await self.discover()
        if not capabilities.get("woocommerce_api"):
            raise ConnectorError("WooCommerce wc/v3 REST API is not available")
        seo_write = bool(capabilities.get("seo", {}).get("write"))
        if write and not (
            capabilities.get("products", {}).get("update")
            or capabilities.get("categories", {}).get("update")
            or seo_write
        ):
            raise ConnectorError("WooCommerce editorial write capability is not available")
        return capabilities

    @staticmethod
    def _item_capabilities(
        capabilities: Mapping[str, Any], endpoint: str
    ) -> Mapping[str, Any]:
        resource = "categories" if endpoint == "products/categories" else "products"
        result = capabilities.get(resource, {})
        return result if isinstance(result, Mapping) else {}

    def _resource_parts(self, resource_key: str) -> tuple[str, str, str]:
        if not isinstance(resource_key, str) or not resource_key.strip():
            raise ResourceNotFound("A WooCommerce resource key is required")
        key = resource_key.strip()
        cached = self._resource_routes.get(key)
        if cached:
            match = re.search(r"/wc/v3/(products(?:/categories)?)/([0-9]+)$", cached.rstrip("/"))
            if match:
                endpoint, remote_id = match.groups()
                return endpoint, remote_id, f"{endpoint.rsplit('/', 1)[0]}" if endpoint.endswith("/categories") else endpoint
        match = re.match(r"^([^:/]+)[:/]([0-9]+)$", key)
        if not match:
            raise ResourceNotFound("WooCommerce resource keys must identify a numeric resource")
        type_name, remote_id = match.groups()
        normalized = type_name.lower()
        if normalized in {"product", "products"}:
            return "products", remote_id, "products"
        if normalized in {"category", "categories", "product_category", "product-category","product_categories"}:
            return "products/categories", remote_id, "products/categories"
        raise ResourceNotFound("Unsupported WooCommerce resource type")

    @staticmethod
    def _resource_type(endpoint: str) -> str:
        return "category" if endpoint == "products/categories" else "product"

    async def _fetch_collection(self, endpoint: str, *, max_pages: int = 100) -> list[dict[str, Any]]:
        return await self._fetch_wp_collection(
            self._woo_path(endpoint), params={"context": "edit"}, max_pages=max_pages,
        )

    def _remember_route(self, record: Mapping[str, Any], endpoint: str) -> None:
        self._resource_routes[str(record["resource_key"])] = f"/wc/v3/{endpoint}/{record['id']}"

    def _editorial_source(
        self,
        raw: Mapping[str, Any],
        endpoint: str,
        seo: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if endpoint == "products/categories":
            source = {
                "resource_type": "category",
                "name": raw.get("name", ""),
                "slug": raw.get("slug", ""),
                "description": _field_text(raw.get("description")),
            }
        else:
            source = {
                "resource_type": "product",
                "name": raw.get("name", ""),
                "description": _field_text(raw.get("description")),
                "short_description": _field_text(raw.get("short_description")),
                "categories": copy_json(raw.get("categories", [])),
                "tags": copy_json(raw.get("tags", [])),
            }
        if seo is not None:
            source["seo"] = copy_json(seo)
        return source

    def _metadata(
        self,
        raw: Mapping[str, Any],
        endpoint: str,
        seo: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if endpoint == "products/categories":
            metadata = {
                "slug": raw.get("slug"),
                "parent": raw.get("parent"),
                "count": raw.get("count"),
            }
            if seo is not None:
                metadata["seo"] = copy_json(seo)
            return json_clone(metadata)
        metadata = {
            "short_description": _field_text(raw.get("short_description")),
            "categories": raw.get("categories", []),
            "tags": raw.get("tags", []),
            "images": raw.get("images", []),
            "type": raw.get("type"),
            "status": raw.get("status"),
        }
        if seo is not None:
            metadata["seo"] = copy_json(seo)
        return json_clone(metadata)

    @staticmethod
    def _seo_from_response(value: Any) -> dict[str, dict[str, str]]:
        if not isinstance(value, Mapping):
            return {}
        raw_values = value.get("seo")
        if not isinstance(raw_values, Mapping):
            return {}
        provider = str(value.get("seo_provider") or "forgeseo").strip().lower()
        provider = {"native": "forgeseo", "forgeseo": "forgeseo"}.get(provider, provider)
        if provider not in {"forgeseo", "yoast", "rank_math"}:
            provider = "forgeseo"
        values = {
            field: _field_text(raw_values.get(field))
            for field in ("title", "description")
            if field in raw_values
        }
        return {provider: values}

    @staticmethod
    def _seo_from_embedded_product(raw: Mapping[str, Any]) -> dict[str, dict[str, str]] | None:
        value = raw.get("forgeseo_seo")
        if not isinstance(value, Mapping):
            return None
        return {
            "forgeseo": {
                field: _field_text(value.get(field))
                for field in ("title", "description")
                if field in value
            }
        }

    def _normalize(
        self,
        raw_value: Mapping[str, Any],
        endpoint: str,
        *,
        seo: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw = json_clone(raw_value)
        remote_id = raw.get("id")
        if remote_id is None:
            raise ConnectorError("WooCommerce resource response did not include an id")
        resource_type = self._resource_type(endpoint)
        resource_key = f"{resource_type}:{remote_id}"
        if endpoint == "products" and seo is None:
            seo = self._seo_from_embedded_product(raw)
        title = str(raw.get("name") or "")
        body = _field_text(raw.get("description"))
        record = {
            "resource_key": resource_key,
            "resource_type": resource_type,
            "id": remote_id,
            "url": raw.get("permalink") or raw.get("image", {}).get("src") if isinstance(raw.get("image"), Mapping) else raw.get("permalink"),
            "title": title,
            "body": body,
            "status": raw.get("status"),
            "metadata": self._metadata(raw, endpoint, seo),
            "source_hash": stable_hash(self._editorial_source(raw, endpoint, seo)),
            "raw": raw,
        }
        return json_clone(record)

    async def _read_resource_seo(
        self,
        endpoint: str,
        remote_id: str,
    ) -> dict[str, dict[str, str]] | None:
        route = self._category_seo_route if endpoint == "products/categories" else self._seo_route
        methods = (
            self._category_seo_route_methods
            if endpoint == "products/categories"
            else self._seo_route_methods
        )
        if not route or "GET" not in methods:
            return None
        payload, _ = await self._json_request("GET", self._seo_path(endpoint, remote_id))
        if not isinstance(payload, Mapping):
            raise ConnectorError("ForgeSEO product SEO response was not an object")
        return self._seo_from_response(payload)

    async def inventory(self) -> list[dict[str, Any]]:
        capabilities = await self._ensure_ready()
        if not capabilities.get("products", {}).get("read"):
            raise ConnectorError("WooCommerce product REST endpoint is not available")
        records: list[dict[str, Any]] = []
        for endpoint in ("products", "products/categories"):
            if endpoint == "products/categories" and not capabilities.get("categories", {}).get("read"):
                continue
            for raw in await self._fetch_collection(endpoint):
                seo = None
                if endpoint == "products" or endpoint == "products/categories":
                    seo = await self._read_resource_seo(endpoint, str(raw["id"]))
                record = self._normalize(raw, endpoint, seo=seo)
                self._remember_route(record, endpoint)
                records.append(record)
        return json_clone(records)

    async def read(self, resource_key: str) -> dict[str, Any]:
        capabilities = await self._ensure_ready()
        endpoint, remote_id, _ = self._resource_parts(resource_key)
        resource_capabilities = self._item_capabilities(capabilities, endpoint)
        item_capabilities = resource_capabilities.get("item")
        if not isinstance(item_capabilities, Mapping) or not item_capabilities.get("read"):
            raise ConnectorError("WooCommerce resource item read capability is not available")
        try:
            payload, _ = await self._json_request("GET", self._woo_path(f"{endpoint}/{remote_id}"),params={'context':'edit'})
        except ConnectorError as exc:
            if exc.status_code == 404:
                raise ResourceNotFound(f"WooCommerce resource {resource_key} was not found") from exc
            raise
        if not isinstance(payload, Mapping):
            raise ConnectorError("WooCommerce resource response was not an object")
        seo = await self._read_resource_seo(endpoint, remote_id)
        record = self._normalize(payload, endpoint, seo=seo)
        self._remember_route(record, endpoint)
        return record

    def _check_protected_nested(self, changes: Any) -> None:
        for path, key in walk_mapping_keys(changes):
            if str(key).lower().replace("-", "_") in _WOO_PROTECTED_NORMALIZED:
                raise ProtectedField(path, "WooCommerce commerce and builder controls are protected")

    @staticmethod
    def _string(field: str, value: Any) -> str:
        if not isinstance(value, str) or "\x00" in value:
            raise UnsupportedField(field, f"WooCommerce field {field} must be a string")
        return value

    def _validate_seo(self, value: Any, endpoint: str) -> dict[str, str]:
        route = self._category_seo_route if endpoint == "products/categories" else self._seo_route
        methods = (
            self._category_seo_route_methods
            if endpoint == "products/categories"
            else self._seo_route_methods
        )
        if not (route and "GET" in methods and methods.intersection({"POST", "PUT", "PATCH"})):
            raise UnsupportedField(
                "seo",
                "SEO writes require the verified ForgeSEO connector for this resource",
            )
        if not isinstance(value, Mapping) or not value:
            raise UnsupportedField("seo", "SEO changes must be a non-empty mapping")
        result: dict[str, str] = {}
        for field, item in value.items():
            name = str(field)
            if name not in {"title", "description"}:
                raise UnsupportedField(f"seo.{name}")
            result[name] = self._string(f"seo.{name}", item)
            if len(result[name]) > 512:
                raise UnsupportedField(f"seo.{name}", "SEO metadata is too long")
        return result

    @staticmethod
    def _woo_terms(field: str, value: Any) -> list[dict[str, int]]:
        if not isinstance(value, list):
            raise UnsupportedField(field, f"WooCommerce field {field} must be a list")
        result: list[dict[str, int]] = []
        for item in value:
            if isinstance(item, Mapping):
                remote_id = safe_int(item.get("id"))
            else:
                remote_id = safe_int(item)
            if remote_id is None or remote_id < 0:
                raise UnsupportedField(field, f"WooCommerce field {field} requires numeric term ids")
            result.append({"id": remote_id})
        return result

    def _validate_changes(
        self,
        changes: Mapping[str, Any],
        current: Mapping[str, Any],
        endpoint: str,
    ) -> dict[str, Any]:
        if not isinstance(changes, Mapping):
            raise UnsupportedField("changes", "Changes must be a mapping")
        self._check_protected_nested(changes)
        is_category = endpoint == "products/categories"
        allowed = {"name", "title", "description", "body", "slug", "seo"} if is_category else {
            "name",
            "title",
            "description",
            "body",
            "short_description",
            "categories",
            "tags",
            "seo",
        }
        result: dict[str, Any] = {}
        for key, value in changes.items():
            name = str(key)
            if name not in allowed:
                raise UnsupportedField(name)
            if name in {"name", "title"}:
                if "name" in result:
                    raise UnsupportedField(name, "Use only one of name or title")
                result["name"] = self._string(name, value)
            elif name in {"description", "body"}:
                if "description" in result:
                    raise UnsupportedField(name, "Use only one of description or body")
                result["description"] = self._string(name, value)
            elif name == "short_description":
                result[name] = self._string(name, value)
            elif name == "slug":
                slug = self._string(name, value)
                if len(slug) > 200 or any(char.isspace() for char in slug):
                    raise UnsupportedField(name, "WooCommerce slugs must be whitespace-free")
                result[name] = slug
            elif name in {"categories", "tags"}:
                result[name] = self._woo_terms(name, value)
            elif name == "seo":
                result[name] = self._validate_seo(value, endpoint)
        if self._builder_present(current.get("raw", {})) and any(field in result for field in {"description", "short_description"}):
            raise ProtectedField("description", "Builder-managed product content is protected")
        return result

    @staticmethod
    def _builder_present(raw: Any) -> bool:
        if not isinstance(raw, Mapping):
            return False

        def contains_marker(value: Any) -> bool:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    normalized_key = str(key).lower().replace("-", "_")
                    if _builder_provider_for_field(normalized_key) or normalized_key in _GENERIC_BUILDER_METADATA_FIELDS:
                        return True
                    # WooCommerce represents metadata as ``{"key": ..., "value": ...}``.
                    # The builder marker is therefore often the value of a key,
                    # not the mapping key itself.
                    if normalized_key in {"key", "name"} and isinstance(child, str):
                        if _builder_provider_for_field(child) or child.lower().replace("-", "_") in _GENERIC_BUILDER_METADATA_FIELDS:
                            return True
                    if contains_marker(child):
                        return True
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return any(contains_marker(child) for child in value)
            return False

        return contains_marker(raw)

    def _snapshot_changes(self, snapshot: Mapping[str, Any], endpoint: str) -> dict[str, Any]:
        raw_value: Any = snapshot.get("raw") if isinstance(snapshot, Mapping) and "raw" in snapshot else snapshot
        if not isinstance(raw_value, Mapping):
            raise ConnectorError("A connector snapshot must contain a JSON object")
        raw = raw_value
        changes: dict[str, Any] = {}
        if "name" in raw:
            changes["name"] = str(raw.get("name") or "")
        if "description" in raw:
            changes["description"] = _field_text(raw.get("description"))
        if endpoint == "products/categories":
            if "slug" in raw:
                changes["slug"] = str(raw.get("slug") or "")
        else:
            if "short_description" in raw:
                changes["short_description"] = _field_text(raw.get("short_description"))
            for field in ("categories", "tags"):
                if isinstance(raw.get(field), list):
                    changes[field] = copy_json(raw[field])
        metadata = snapshot.get("metadata") if isinstance(snapshot, Mapping) else None
        seo = metadata.get("seo") if isinstance(metadata, Mapping) else None
        if isinstance(seo, Mapping):
            for provider_values in seo.values():
                if isinstance(provider_values, Mapping):
                    restored = {
                        field: _field_text(provider_values.get(field))
                        for field in ("title", "description")
                        if field in provider_values
                    }
                    if restored:
                        changes["seo"] = restored
                        break
        return changes

    def _native_rollback_payload(
        self,
        current: Mapping[str, Any],
        native_payload: Mapping[str, Any],
        endpoint: str,
    ) -> dict[str, Any]:
        """Build a validated inverse for a catalog write before a second route."""

        snapshot_changes = self._snapshot_changes(current, endpoint)
        inverse_changes: dict[str, Any] = {}
        for field in native_payload:
            if field not in snapshot_changes:
                raise ConnectorError(
                    f"WooCommerce rollback could not capture catalog field {field}"
                )
            inverse_changes[str(field)] = snapshot_changes[field]
        inverse_payload = self._validate_changes(inverse_changes, current, endpoint)
        if set(inverse_payload) != set(native_payload):
            raise ConnectorError("WooCommerce rollback did not cover the catalog write")
        return inverse_payload

    async def _write_seo(
        self,
        endpoint: str,
        remote_id: str,
        payload: Mapping[str, str],
    ) -> Any:
        methods = (
            self._category_seo_route_methods
            if endpoint == "products/categories"
            else self._seo_route_methods
        )
        method = next(
            (candidate for candidate in ("POST", "PUT", "PATCH") if candidate in methods),
            None,
        )
        if method is None:
            raise UnsupportedField("seo", "ForgeSEO product SEO route is not writable")
        try:
            result, _ = await self._json_request(
                method,
                self._seo_path(endpoint, remote_id),
                json=dict(payload),
            )
        except ConnectorError as exc:
            response_was_successful_but_unreadable = (
                exc.response_received
                and exc.status_code is not None
                and 200 <= exc.status_code < 300
            )
            if not exc.transport_error and not response_was_successful_but_unreadable:
                raise
            raise AmbiguousOutcome(
                "WooCommerce SEO write outcome is unknown and needs reconciliation"
            ) from exc
        return result

    async def _write(
        self,
        endpoint: str,
        remote_id: str,
        payload: Mapping[str, Any],
    ) -> tuple[Any, httpx.Response]:
        try:
            return await self._json_request(
                "PUT",
                self._woo_path(f"{endpoint}/{remote_id}"),
                json=dict(payload),
            )
        except ConnectorError as exc:
            response_was_successful_but_unreadable = (
                exc.response_received
                and exc.status_code is not None
                and 200 <= exc.status_code < 300
            )
            if not exc.transport_error and not response_was_successful_but_unreadable:
                raise
            raise AmbiguousOutcome(
                "WooCommerce catalog write outcome is unknown and needs reconciliation"
            ) from exc

    @staticmethod
    def _term_ids(value: Any) -> list[int] | None:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            return None
        result: list[int] = []
        for item in value:
            remote_id = safe_int(item.get("id")) if isinstance(item, Mapping) else safe_int(item)
            if remote_id is None:
                return None
            result.append(remote_id)
        return result

    @classmethod
    def _native_payload_matches(
        cls,
        record: Mapping[str, Any],
        payload: Mapping[str, Any],
        endpoint: str,
    ) -> bool:
        """Confirm that the authenticated read reflects every native field sent."""

        raw = record.get("raw")
        if not isinstance(raw, Mapping):
            return False
        for field, expected in payload.items():
            if field in {"name", "description", "short_description", "slug"}:
                if _field_text(raw.get(field)) != _field_text(expected):
                    return False
            elif field in {"categories", "tags"} and endpoint == "products":
                if cls._term_ids(raw.get(field)) != cls._term_ids(expected):
                    return False
            else:  # pragma: no cover - _validate_changes owns this allowlist.
                return False
        return True

    async def _recheck_source_before_write(
        self,
        resource_key: str,
        expected: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Refuse an editorial write after a concurrent source change.

        WooCommerce commerce state is deliberately not part of the editorial
        hash because the connector sends only an editorial allowlist.  Builder
        detection is checked separately so a builder appearing after the
        initial read cannot turn a safe description write into a layout write.
        """

        observed = await self.read(resource_key)
        expected_hash = expected.get("source_hash")
        expected_builder = self._builder_present(expected.get("raw", {}))
        observed_builder = self._builder_present(observed.get("raw", {}))
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

    @staticmethod
    def _record_seo(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
        metadata = record.get("metadata")
        seo = metadata.get("seo") if isinstance(metadata, Mapping) else None
        return seo if isinstance(seo, Mapping) else None

    def _source_hash_excluding(
        self,
        record: Mapping[str, Any],
        endpoint: str,
        native_payload: Mapping[str, Any],
        seo_payload: Mapping[str, str] | None,
    ) -> str | None:
        """Fingerprint the editorial source while excluding intended writes."""

        raw = record.get("raw")
        if not isinstance(raw, Mapping):
            return None
        source = self._editorial_source(raw, endpoint, self._record_seo(record))
        for field in native_payload:
            source.pop(field, None)
        if seo_payload:
            seo = source.get("seo")
            if isinstance(seo, Mapping):
                seo = copy_json(seo)
                values = seo.get("forgeseo")
                if isinstance(values, Mapping):
                    values = copy_json(values)
                    for field in seo_payload:
                        values.pop(field, None)
                    if values:
                        seo["forgeseo"] = values
                    else:
                        seo.pop("forgeseo", None)
                if seo:
                    source["seo"] = seo
                else:
                    source.pop("seo", None)
        return stable_hash(source)

    async def _reconcile_native_timeout(
        self,
        resource_key: str,
        current: Mapping[str, Any],
        payload: Mapping[str, Any],
        endpoint: str,
    ) -> dict[str, Any]:
        """Reconcile an ambiguous catalog write without retrying it."""

        try:
            after = await self.read(resource_key)
        except Exception as exc:
            raise AmbiguousOutcome(
                "WooCommerce catalog write outcome is unknown and its source could not be reconciled"
            ) from exc
        if not self._native_payload_matches(after, payload, endpoint):
            raise AmbiguousOutcome(
                "WooCommerce catalog write outcome is unknown and read-after-write verification did not match"
            )
        if self._source_hash_excluding(current, endpoint, payload, None) != self._source_hash_excluding(
            after,
            endpoint,
            payload,
            None,
        ):
            raise AmbiguousOutcome(
                "WooCommerce source changed outside the requested catalog fields while reconciling the write"
            )
        return after

    @staticmethod
    def _seo_payload_matches(
        observed: Mapping[str, Any] | None,
        payload: Mapping[str, str],
    ) -> bool:
        if not isinstance(observed, Mapping):
            return False
        values = observed.get("forgeseo")
        if not isinstance(values, Mapping):
            return False
        return all(
            _field_text(values.get(field)) == expected
            for field, expected in payload.items()
        )

    async def _apply_loaded(
        self,
        resource_key: str,
        endpoint: str,
        remote_id: str,
        current: Mapping[str, Any],
        payload: Mapping[str, Any],
        seo_payload: Mapping[str, str] | None,
        *,
        source_checked: bool = False,
    ) -> dict[str, Any]:
        native_after: dict[str, Any] | None = None
        if payload:
            write_base = (
                current
                if source_checked
                else await self._recheck_source_before_write(resource_key, current)
            )
            try:
                await self._write(endpoint, remote_id, payload)
            except AmbiguousOutcome:
                await self._reconcile_native_timeout(
                    resource_key,
                    write_base,
                    payload,
                    endpoint,
                )
                # A lost or unreadable response remains ambiguous even when
                # the follow-up read happens to match.  It is evidence for a
                # reconciliation decision, not proof that the original write
                # completed safely enough to auto-advance the workflow.
                raise AmbiguousOutcome(
                    "WooCommerce catalog write outcome is unknown and needs reconciliation"
                )
            else:
                try:
                    native_after = await self.read(resource_key)
                except Exception as exc:
                    raise AmbiguousOutcome(
                        "Native WooCommerce write succeeded but its source could not be verified"
                    ) from exc
                if not self._native_payload_matches(native_after, payload, endpoint):
                    raise AmbiguousOutcome(
                        "Native WooCommerce write succeeded but read-after-write verification did not match"
                    )
            if self._source_hash_excluding(
                write_base, endpoint, payload, None
            ) != self._source_hash_excluding(native_after, endpoint, payload, None):
                raise AmbiguousOutcome(
                    "WooCommerce source changed outside the requested catalog fields"
                )
        if seo_payload:
            try:
                seo_base = native_after if native_after is not None else current
                await self._recheck_source_before_write(resource_key, seo_base)
                await self._write_seo(endpoint, remote_id, seo_payload)
                seo_after = await self._read_resource_seo(endpoint, remote_id)
                if not self._seo_payload_matches(seo_after, seo_payload):
                    raise AmbiguousOutcome(
                        "WooCommerce SEO write succeeded but read-after-write verification did not match"
                    )
                final_after = await self.read(resource_key)
                if payload and not self._native_payload_matches(final_after, payload, endpoint):
                    raise AmbiguousOutcome(
                        "WooCommerce catalog source changed during SEO write verification"
                    )
                final_seo = self._record_seo(final_after)
                if not self._seo_payload_matches(final_seo, seo_payload):
                    raise AmbiguousOutcome(
                        "WooCommerce SEO source changed during final verification"
                    )
                if self._source_hash_excluding(
                    current, endpoint, payload, seo_payload
                ) != self._source_hash_excluding(
                    final_after, endpoint, payload, seo_payload
                ):
                    raise AmbiguousOutcome(
                        "WooCommerce source changed outside the requested fields"
                    )
            except Exception as seo_error:
                if not payload or native_after is None:
                    raise
                try:
                    observed = await self.read(resource_key)
                    if observed.get("source_hash") != native_after.get("source_hash"):
                        raise ConnectorError(
                            "WooCommerce SEO write outcome changed the catalog source"
                        )
                    inverse_payload = self._native_rollback_payload(
                        current,
                        payload,
                        endpoint,
                    )
                    await self._recheck_source_before_write(resource_key, native_after)
                    await self._write(endpoint, remote_id, inverse_payload)
                    restored = await self.read(resource_key)
                    if restored.get("source_hash") != current.get("source_hash"):
                        raise ConnectorError(
                            "WooCommerce catalog compensation did not restore the source"
                        )
                except Exception as compensation_error:
                    raise AmbiguousOutcome(
                        "WooCommerce mutation may be partially applied and needs reconciliation"
                    ) from compensation_error
                raise seo_error
            return final_after
        if native_after is not None and not seo_payload:
            return native_after
        return await self.read(resource_key)

    async def update(
        self,
        resource_key: str,
        changes: dict[str, Any],
        expected_hash: str,
    ) -> dict[str, Any]:
        capabilities = await self._ensure_ready(write=True)
        if not isinstance(expected_hash, str) or not expected_hash:
            raise SourceConflict(str(expected_hash), None, resource_key=resource_key)
        endpoint, remote_id, _ = self._resource_parts(resource_key)
        if endpoint == "products/categories":
            category_seo_write = bool(
                self._category_seo_route
                and "GET" in self._category_seo_route_methods
                and self._category_seo_route_methods.intersection({"POST", "PUT", "PATCH"})
            )
            if not (capabilities.get("categories", {}).get("update") or category_seo_write):
                raise ConnectorError("WooCommerce category write capability is not available")
        elif not (
            capabilities.get("products", {}).get("update")
            or (
                self._seo_route
                and "GET" in self._seo_route_methods
                and self._seo_route_methods.intersection({"POST", "PUT", "PATCH"})
            )
        ):
            raise ConnectorError("WooCommerce product write capability is not available")
        current = await self.read(resource_key)
        if current.get("source_hash") != expected_hash:
            raise SourceConflict(expected_hash, current.get("source_hash"), resource_key=resource_key)
        payload = self._validate_changes(changes, current, endpoint)
        seo_payload = payload.pop("seo", None)
        if not payload and not seo_payload:
            return json_clone(current)
        if payload:
            catalog_capabilities = (
                capabilities.get("categories", {})
                if endpoint == "products/categories"
                else capabilities.get("products", {})
            )
            if not catalog_capabilities.get("update"):
                raise ConnectorError("WooCommerce catalog write capability is not available for this resource")
        return await self._apply_loaded(
            resource_key,
            endpoint,
            remote_id,
            current,
            payload,
            seo_payload,
            source_checked=True,
        )

    async def restore(
        self,
        resource_key: str,
        snapshot: dict[str, Any],
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        capabilities = await self._ensure_ready(write=True)
        endpoint, remote_id, _ = self._resource_parts(resource_key)
        current = await self.read(resource_key)
        if expected_hash is not None and current.get("source_hash") != expected_hash:
            raise SourceConflict(expected_hash, current.get("source_hash"), resource_key=resource_key)
        snapshot_changes = self._snapshot_changes(snapshot, endpoint)
        catalog_capabilities = (
            capabilities.get("categories", {})
            if endpoint == "products/categories"
            else capabilities.get("products", {})
        )
        if not catalog_capabilities.get("update"):
            # A metadata-only connector can roll back the metadata it owns,
            # but must never replay the catalog snapshot through WooCommerce.
            snapshot_changes = {key: value for key, value in snapshot_changes.items() if key == "seo"}
        payload = self._validate_changes(snapshot_changes, current, endpoint)
        seo_payload = payload.pop("seo", None)
        if not payload and not seo_payload:
            return current
        if payload:
            catalog_capabilities = (
                capabilities.get("categories", {})
                if endpoint == "products/categories"
                else capabilities.get("products", {})
            )
            if not catalog_capabilities.get("update"):
                raise ConnectorError("WooCommerce catalog write capability is not available for this resource")
        return await self._apply_loaded(
            resource_key,
            endpoint,
            remote_id,
            current,
            payload,
            seo_payload,
        )


__all__ = ["WooCommerceClient"]
