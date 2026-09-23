"""Focused offline coverage for WooCommerce source-lock continuity."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.connectors import AmbiguousOutcome, WooCommerceClient


def _response(request: httpx.Request, value: object) -> httpx.Response:
    return httpx.Response(200, json=value, request=request)


def _index() -> dict[str, Any]:
    return {
        "namespaces": ["wc/v3"],
        "routes": {
            "/wc/v3/products": {"methods": ["GET"]},
            "/wc/v3/products/(?P<id>[\\d]+)": {"methods": ["GET", "PUT"]},
        },
    }


@pytest.mark.asyncio
async def test_catalog_write_rejects_unrelated_editorial_change_after_write() -> None:
    product: dict[str, Any] = {
        "id": 5,
        "name": "Brake service kit",
        "description": "Original description",
        "short_description": "Original short description",
        "categories": [{"id": 2, "name": "Parts"}],
        "tags": [],
        "price": "99.00",
        "stock_quantity": 7,
        "sku": "SKU-5",
    }
    catalog_writes: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/wp-json/":
            return _response(request, _index())
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, product)
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            payload = json.loads(request.content)
            catalog_writes.append(payload)
            product.update(payload)
            # Simulate an unrelated editor changing the title in the same
            # mutation window. The requested description itself is present.
            product["name"] = "External editor title"
            return _response(request, product)
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://source-lock.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        original = await client.read("product:5")
        with pytest.raises(AmbiguousOutcome, match="outside the requested catalog fields"):
            await client.update(
                "product:5",
                {"description": "Updated description"},
                original["source_hash"],
            )

    assert catalog_writes == [{"description": "Updated description"}]
    assert product["name"] == "External editor title"


@pytest.mark.asyncio
async def test_catalog_write_timeout_is_ambiguous_without_retrying_or_touching_commerce_data() -> None:
    product: dict[str, Any] = {
        "id": 5,
        "name": "Brake service kit",
        "description": "Original description",
        "short_description": "Original short description",
        "categories": [{"id": 2, "name": "Parts"}],
        "tags": [],
        "price": "99.00",
        "stock_quantity": 7,
        "sku": "SKU-5",
    }
    catalog_writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal catalog_writes
        if request.url.path == "/wp-json/":
            return _response(request, _index())
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, product)
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            catalog_writes += 1
            product["description"] = "Updated description"
            raise httpx.ReadTimeout("catalog response lost")
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://timeout.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        original = await client.read("product:5")
        with pytest.raises(AmbiguousOutcome, match="outcome is unknown"):
            await client.update(
                "product:5",
                {"description": "Updated description"},
                original["source_hash"],
            )

    assert catalog_writes == 1
    assert product["description"] == "Updated description"
    assert product["price"] == "99.00"
    assert product["stock_quantity"] == 7
    assert product["sku"] == "SKU-5"


@pytest.mark.asyncio
async def test_successful_mutation_with_malformed_json_is_ambiguous_without_retry() -> None:
    product: dict[str, Any] = {
        "id": 5,
        "name": "Brake service kit",
        "description": "Original description",
        "short_description": "Original short description",
        "categories": [{"id": 2, "name": "Parts"}],
        "tags": [],
        "price": "99.00",
        "stock_quantity": 7,
        "sku": "SKU-5",
    }
    catalog_writes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal catalog_writes
        if request.url.path == "/wp-json/":
            return _response(request, _index())
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "GET":
            return _response(request, product)
        if request.url.path == "/wp-json/wc/v3/products/5" and request.method == "PUT":
            catalog_writes += 1
            product["description"] = "Updated description"
            return httpx.Response(
                200,
                content=b"<html>updated</html>",
                request=request,
            )
        pytest.fail(f"unexpected request: {request.method} {request.url}")

    async with WooCommerceClient(
        "https://malformed-success.woocommerce.test",
        {"consumer_key": "ck", "consumer_secret": "cs"},
        transport=httpx.MockTransport(handler),
    ) as client:
        original = await client.read("product:5")
        with pytest.raises(AmbiguousOutcome, match="outcome is unknown"):
            await client.update(
                "product:5",
                {"description": "Updated description"},
                original["source_hash"],
            )

    assert catalog_writes == 1
    assert product["description"] == "Updated description"
    assert product["price"] == "99.00"
    assert product["stock_quantity"] == 7
    assert product["sku"] == "SKU-5"
