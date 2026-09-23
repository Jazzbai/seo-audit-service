from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.connectors import (
    AmbiguousOutcome,
    ConnectorError,
    ProtectedField,
    UnsupportedField,
    WooCommerceClient,
)


def _response(request: httpx.Request, value: object) -> httpx.Response:
    return httpx.Response(200, json=value, request=request)


def _routes(
    *,
    product_item_methods: list[str] | None = None,
    category_item_methods: list[str] | None = None,
    product_builder: bool = False,
    product_seo_methods: list[str] | None = None,
    category_seo_methods: list[str] | None = None,
) -> dict[str, Any]:
    product_collection: dict[str, Any] = {"methods": ["GET"]}
    if product_builder:
        product_collection["endpoints"] = [
            {
                "methods": ["GET"],
                "args": {
                    "meta_data": {
                        "type": "array",
                        "items": {
                            "properties": {
                                "_elementor_data": {"type": "string"},
                            }
                        },
                    }
                },
            }
        ]

    routes: dict[str, Any] = {
        "/wc/v3/products": product_collection,
        "/wc/v3/products/categories": {"methods": ["GET"]},
    }
    if product_item_methods is not None:
        routes["/wc/v3/products/(?P<id>[\\d]+)"] = {
            "methods": product_item_methods
        }
    if category_item_methods is not None:
        routes["/wc/v3/products/categories/(?P<id>[\\d]+)"] = {
            "methods": category_item_methods
        }
    if product_seo_methods is not None:
        routes["/forgeseo/v1/products/(?P<id>[\\d]+)/seo"] = {
            "methods": product_seo_methods
        }
    if category_seo_methods is not None:
        routes["/forgeseo/v1/product-categories/(?P<id>[\\d]+)/seo"] = {
            "methods": category_seo_methods
        }
    return routes


def _index(**kwargs: Any) -> dict[str, Any]:
    return {
        "name": "Woo capability fixture",
        "namespaces": ["wc/v3"],
        "routes": _routes(**kwargs),
    }


def _product(*, builder: bool = False) -> dict[str, Any]:
    return {
        "id": 5,
        "name": "Brake service kit",
        "permalink": "https://example.test/product/brake-service-kit",
        "status": "publish",
        "description": "Product description",
        "short_description": "Short description",
        "categories": [{"id": 2, "name": "Parts"}],
        "tags": [],
        "price": "99.00",
        "stock_quantity": 7,
        "sku": "SKU-5",
        "variations": [11, 12],
        "meta_data": (
            [{"id": 1, "key": "_elementor_data", "value": "serialized"}]
            if builder
            else [{"id": 1, "key": "_private_store_flag", "value": "keep"}]
        ),
    }


async def _discover(index: dict[str, Any]) -> dict[str, Any]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, index)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://capabilities.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        return await client.discover()


@pytest.mark.asyncio
async def test_discovery_reports_collection_read_and_item_update_for_products_and_categories() -> None:
    capabilities = await _discover(
        _index(
            product_item_methods=["GET", "PUT"],
            category_item_methods=["GET", "PUT"],
        )
    )

    assert capabilities["products"]["collection"] == {"read": True}
    assert capabilities["products"]["item"] == {"read": True, "update": True}
    assert capabilities["products"]["read"] is True
    assert capabilities["products"]["update"] is True
    assert capabilities["categories"]["collection"] == {"read": True}
    assert capabilities["categories"]["item"] == {"read": True, "update": True}
    assert capabilities["categories"]["read"] is True
    assert capabilities["categories"]["update"] is True

    assert capabilities["supported_editorial_fields"] == {
        "product": ["name", "description", "short_description", "categories", "tags"],
        "category": ["name", "description", "slug"],
    }
    assert capabilities["products"]["editorial_fields"] == [
        "name",
        "description",
        "short_description",
        "categories",
        "tags",
    ]
    assert {"price", "stock_quantity", "sku", "variations", "meta_data"}.issubset(
        capabilities["protected_commerce_fields"]
    )
    assert capabilities["protected_commerce_resources"] == ["orders", "payments", "customers"]


@pytest.mark.asyncio
async def test_discovery_does_not_promote_collection_or_post_only_routes_to_put_updates() -> None:
    capabilities = await _discover(
        _index(
            product_item_methods=["GET", "POST"],
            category_item_methods=None,
        )
    )

    assert capabilities["products"]["collection"]["read"] is True
    assert capabilities["products"]["item"] == {"read": True, "update": False}
    assert capabilities["products"]["update"] is False
    assert capabilities["categories"]["collection"]["read"] is True
    assert capabilities["categories"]["item"] == {"read": False, "update": False}
    assert capabilities["categories"]["update"] is False


@pytest.mark.asyncio
async def test_discovery_does_not_promote_slug_item_routes_to_catalog_updates() -> None:
    index = _index(
        product_item_methods=["GET", "PUT"],
        category_item_methods=["GET", "PUT"],
    )
    index["routes"].pop("/wc/v3/products/(?P<id>[\\d]+)")
    index["routes"]["/wc/v3/products/(?P<slug>[^/]+)"] = {
        "methods": ["GET", "PUT"],
    }
    index["routes"].pop("/wc/v3/products/categories/(?P<id>[\\d]+)")
    index["routes"]["/wc/v3/products/categories/(?P<slug>[^/]+)"] = {
        "methods": ["GET", "PUT"],
    }

    capabilities = await _discover(index)

    assert capabilities["products"]["item"] == {"read": False, "update": False}
    assert capabilities["products"]["update"] is False
    assert capabilities["categories"]["item"] == {"read": False, "update": False}
    assert capabilities["categories"]["update"] is False


@pytest.mark.asyncio
async def test_discovery_ignores_seo_routes_without_a_verified_numeric_id() -> None:
    index = _index(
        product_item_methods=["GET"],
        category_item_methods=["GET"],
        product_seo_methods=["GET", "POST"],
        category_seo_methods=["GET", "POST"],
    )
    index["routes"].pop("/forgeseo/v1/products/(?P<id>[\\d]+)/seo")
    index["routes"]["/forgeseo/v1/products/(?P<slug>[^/]+)/seo"] = {
        "methods": ["GET", "POST"]
    }
    index["routes"].pop("/forgeseo/v1/product-categories/(?P<id>[\\d]+)/seo")
    index["routes"]["/forgeseo/v1/product-categories/{slug}/seo"] = {
        "methods": ["GET", "POST"]
    }

    capabilities = await _discover(index)

    assert capabilities["seo"] == {
        "detected": False,
        "read": False,
        "write": False,
        "provider": None,
        "writable_fields": [],
        "resource_types": [],
        "route": None,
    }
    assert capabilities["products"]["update"] is False
    assert capabilities["categories"]["update"] is False


@pytest.mark.asyncio
async def test_verified_product_seo_route_is_read_and_written_separately_from_catalog() -> None:
    product = _product()
    seo = {"title": "", "description": ""}
    catalog_puts: list[dict[str, Any]] = []
    seo_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(
                request,
                _index(
                    product_item_methods=["GET"],
                    category_item_methods=None,
                    product_seo_methods=["GET", "POST"],
                ),
            )
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, product)
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            catalog_puts.append({})
            pytest.fail("product SEO update attempted to write the WooCommerce catalog")
        if request.url.path == "/wp-json/forgeseo/v1/products/5/seo" and request.method == "GET":
            return _response(
                request,
                {
                    "resource_type": "product",
                    "id": 5,
                    "seo_provider": "native",
                    "seo": seo,
                },
            )
        if request.url.path == "/wp-json/forgeseo/v1/products/5/seo" and request.method == "POST":
            body = request.content.decode("utf-8")
            payload = json.loads(body)
            seo.update(payload)
            seo_posts.append(payload)
            return _response(
                request,
                {
                    "resource_type": "product",
                    "id": 5,
                    "seo_provider": "native",
                    "seo": seo,
                },
            )
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://product-seo.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        capabilities = await client.discover()
        assert capabilities["seo"] == {
            "detected": True,
            "read": True,
            "write": True,
            "provider": "forgeseo",
            "writable_fields": ["title", "description"],
            "resource_types": ["product"],
            "route": "/forgeseo/v1/products/(?P<id>[\\d]+)/seo",
        }
        record = await client.read("product:5")
        assert record["metadata"]["seo"]["forgeseo"] == {
            "title": "",
            "description": "",
        }
        with pytest.raises(ConnectorError):
            await client.update(
                "product:5",
                {
                    "seo": {"title": "Would be unsafe"},
                    "name": "Catalog write is not authorized",
                },
                record["source_hash"],
            )
        changed = await client.update(
            "product:5",
            {"seo": {"title": "Brake service kit | Auto1StopShop"}},
            record["source_hash"],
        )
        restored = await client.restore("product:5", record, changed["source_hash"])

    assert catalog_puts == []
    assert seo_posts == [
        {"title": "Brake service kit | Auto1StopShop"},
        {"title": "", "description": ""},
    ]
    assert changed["metadata"]["seo"]["forgeseo"]["title"] == "Brake service kit | Auto1StopShop"
    assert restored["metadata"]["seo"]["forgeseo"] == {"title": "", "description": ""}


@pytest.mark.asyncio
async def test_seo_only_write_does_not_trust_stale_mutation_response() -> None:
    product = _product()
    seo = {"title": "", "description": ""}
    seo_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(
                request,
                _index(
                    product_item_methods=["GET"],
                    category_item_methods=None,
                    product_seo_methods=["GET", "POST"],
                ),
            )
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, product)
        if request.url.path == "/wp-json/forgeseo/v1/products/5/seo" and request.method == "GET":
            # The remote route remains stale after the mutation response.
            return _response(
                request,
                {"resource_type": "product", "seo_provider": "native", "seo": seo},
            )
        if request.url.path == "/wp-json/forgeseo/v1/products/5/seo" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            seo_posts.append(payload)
            return _response(
                request,
                {
                    "resource_type": "product",
                    "seo_provider": "native",
                    "seo": {**seo, **payload},
                },
            )
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://stale-seo.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        record = await client.read("product:5")
        with pytest.raises(AmbiguousOutcome, match="SEO write succeeded"):
            await client.update(
                "product:5",
                {"seo": {"title": "Fresh title"}},
                record["source_hash"],
            )

    assert seo_posts == [{"title": "Fresh title"}]


@pytest.mark.asyncio
async def test_mixed_catalog_and_seo_failure_compensates_catalog_write() -> None:
    product = _product()
    seo = {"title": "", "description": ""}
    catalog_writes: list[dict[str, Any]] = []
    seo_writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seo_writes
        if request.url.path == "/wp-json/":
            return _response(
                request,
                _index(
                    product_item_methods=["GET", "PUT"],
                    category_item_methods=None,
                    product_seo_methods=["GET", "POST"],
                ),
            )
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, product)
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            changes = json.loads(request.content)
            catalog_writes.append(changes)
            product.update(changes)
            return _response(request, product)
        if request.url.path == "/wp-json/forgeseo/v1/products/5/seo" and request.method == "GET":
            return _response(
                request,
                {"resource_type": "product", "seo_provider": "native", "seo": seo},
            )
        if request.url.path == "/wp-json/forgeseo/v1/products/5/seo" and request.method == "POST":
            seo_writes += 1
            return httpx.Response(500, json={"code": "fixture_seo_failure"}, request=request)
        pytest.fail(f"unexpected mixed-write request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://mixed-write.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        original = await client.read("product:5")
        with pytest.raises(ConnectorError):
            await client.update(
                "product:5",
                {"description": "New description", "seo": {"title": "New SEO title"}},
                original["source_hash"],
            )
        observed = await client.read("product:5")

    assert catalog_writes == [
        {"description": "New description"},
        {"description": "Product description"},
    ]
    assert seo_writes == 1
    assert observed["body"] == original["body"]
    assert observed["metadata"]["seo"]["forgeseo"] == {"title": "", "description": ""}


@pytest.mark.asyncio
async def test_verified_product_category_seo_route_is_read_and_written_separately_from_catalog() -> None:
    category = {
        "id": 2,
        "name": "Parts",
        "slug": "parts",
        "description": "Parts category",
        "count": 1,
    }
    seo = {"title": "", "description": ""}
    catalog_puts: list[dict[str, Any]] = []
    seo_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(
                request,
                _index(
                    product_item_methods=["GET"],
                    category_item_methods=["GET"],
                    product_seo_methods=None,
                    category_seo_methods=["GET", "POST"],
                ),
            )
        if request.url.path == "/wp-json/wc/v3/products/categories/2" and request.method == "GET":
            return _response(request, category)
        if request.url.path == "/wp-json/wc/v3/products/categories/2" and request.method == "PUT":
            catalog_puts.append({})
            pytest.fail("category SEO update attempted to write the WooCommerce catalog")
        if request.url.path == "/wp-json/forgeseo/v1/product-categories/2/seo" and request.method == "GET":
            return _response(
                request,
                {
                    "resource_type": "product_category",
                    "id": 2,
                    "seo_provider": "native",
                    "seo": seo,
                },
            )
        if request.url.path == "/wp-json/forgeseo/v1/product-categories/2/seo" and request.method == "POST":
            payload = json.loads(request.content.decode("utf-8"))
            seo.update(payload)
            seo_posts.append(payload)
            return _response(
                request,
                {
                    "resource_type": "product_category",
                    "id": 2,
                    "seo_provider": "native",
                    "seo": seo,
                },
            )
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://category-seo.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        capabilities = await client.discover()
        assert capabilities["seo"]["resource_types"] == ["category"]
        assert capabilities["seo"]["category_route"] == "/forgeseo/v1/product-categories/(?P<id>[\\d]+)/seo"
        record = await client.read("category:2")
        assert record["metadata"]["seo"]["forgeseo"] == {
            "title": "",
            "description": "",
        }
        changed = await client.update(
            "category:2",
            {"seo": {"description": "Browse verified automotive parts and services."}},
            record["source_hash"],
        )

    assert catalog_puts == []
    assert seo_posts == [{"description": "Browse verified automotive parts and services."}]
    assert changed["metadata"]["seo"]["forgeseo"]["description"] == "Browse verified automotive parts and services."


@pytest.mark.asyncio
async def test_discovery_reports_builder_content_constraints_without_promoting_metadata_writes() -> None:
    capabilities = await _discover(
        _index(
            product_item_methods=["GET", "PUT"],
            category_item_methods=["GET", "PUT"],
            product_builder=True,
        )
    )

    product_constraints = capabilities["products"]["builder_constraints"]
    assert product_constraints["detected"] is True
    assert product_constraints["providers"] == ["elementor"]
    assert product_constraints["metadata_fields"] == ["_elementor_data"]
    assert product_constraints["detection_source"] == "woocommerce_rest_route_metadata"
    assert product_constraints["metadata_write"] is False
    assert product_constraints["content_write"] == {
        "mode": "protected_when_builder_metadata_present",
        "allowed_for_unmanaged_target": True,
        "blocked_for_builder_target": True,
        "requires_target_check": True,
    }
    assert capabilities["editorial"]["product"]["builder_constraints"] == product_constraints
    assert capabilities["editorial"]["product"]["writable_fields"]

    category_constraints = capabilities["categories"]["builder_constraints"]
    assert category_constraints["detected"] is False
    assert capabilities["editorial"]["category"]["builder_constraints"] == category_constraints


@pytest.mark.asyncio
async def test_protected_commerce_and_arbitrary_meta_fields_never_reach_put() -> None:
    product = _product()
    put_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(
                request,
                _index(product_item_methods=["GET", "PUT"], category_item_methods=["GET", "PUT"]),
            )
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, product)
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            put_bodies.append(dict(request.read()))
            pytest.fail("protected-field test attempted a WooCommerce write")
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://protected.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        record = await client.read("product:5")
        for changes in (
            {"price": "0.01"},
            {"stock_quantity": 0},
            {"sku": "changed"},
            {"variations": []},
            {"orders": [{"id": 1}]},
            {"payment": {"status": "paid"}},
            {"customer": {"id": 1}},
            {"meta_data": [{"key": "arbitrary", "value": "write"}]},
        ):
            with pytest.raises(ProtectedField):
                await client.update("product:5", changes, record["source_hash"])

        with pytest.raises(UnsupportedField):
            await client.update(
                "product:5",
                {"arbitrary_meta": {"key": "value"}},
                record["source_hash"],
            )

    assert put_bodies == []


@pytest.mark.asyncio
async def test_builder_markers_on_a_product_block_content_write_after_target_read() -> None:
    product = _product(builder=True)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(
                request,
                _index(product_item_methods=["GET", "PUT"], category_item_methods=["GET", "PUT"]),
            )
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, product)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://builder.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        record = await client.read("product:5")
        with pytest.raises(ProtectedField):
            await client.update("product:5", {"description": "replace builder"}, record["source_hash"])


@pytest.mark.asyncio
async def test_sold_individually_is_not_misclassified_as_divi_builder_metadata() -> None:
    product = _product()
    product["sold_individually"] = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(
                request,
                _index(product_item_methods=["GET", "PUT"], category_item_methods=["GET", "PUT"]),
            )
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, product)
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            product["description"] = "updated"
            return _response(request, product)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://ordinary.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        record = await client.read("product:5")
        assert client._builder_present(record["raw"]) is False
        changed = await client.update("product:5", {"description": "updated"}, record["source_hash"])
        assert changed["body"] == "updated"


@pytest.mark.asyncio
async def test_native_catalog_write_does_not_trust_stale_mutation_response() -> None:
    product = _product()
    get_calls = 0
    put_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_calls, put_calls
        if request.url.path == "/wp-json/":
            return _response(
                request,
                _index(product_item_methods=["GET", "PUT"], category_item_methods=["GET", "PUT"]),
            )
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            get_calls += 1
            return _response(request, product)
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            put_calls += 1
            # Simulate a server response that claims success while the
            # subsequent authenticated read still exposes the old source.
            return _response(request, {**product, "description": "updated"})
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://stale-write.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        record = await client.read("product:5")
        with pytest.raises(AmbiguousOutcome, match="read-after-write verification"):
            await client.update("product:5", {"description": "updated"}, record["source_hash"])

    assert get_calls == 3  # initial read, update precondition, and mandatory read-back
    assert put_calls == 1
