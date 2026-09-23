"""Collection coverage must be trustworthy before inventory reconciliation."""
import httpx
import pytest

from app.connectors.errors import IncompleteInventory
from app.connectors.wordpress import WordPressClient
from app.connectors.woocommerce import WooCommerceClient


@pytest.fixture(params=[(WordPressClient, "posts"), (WooCommerceClient, "products"),
                       (WooCommerceClient, "products/categories")])
def collection(request):
    return request.param


async def collect(collection, handler, *, max_pages=100):
    client_type, endpoint = collection
    requests = []

    def transport(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.params["per_page"] == "100"
        page = int(request.url.params["page"])
        payload, headers = handler(page)
        return httpx.Response(200, json=payload, headers=headers, request=request)

    async with client_type("https://fixture.test", {"token": "fixture-only"},
                           transport=httpx.MockTransport(transport)) as client:
        items = await client._fetch_collection(endpoint, max_pages=max_pages)
    return items, requests


def batch(page, count=100):
    return [{"id": (page - 1) * 100 + index + 1} for index in range(count)]


@pytest.mark.asyncio
async def test_inventory_larger_than_old_default_cap_is_fully_enumerated(collection):
    # 1,201 records previously used 121 default-size WP pages, beyond the cap.
    items, requests = await collect(collection, lambda page: (
        batch(page, 1 if page == 13 else 100),
        {"X-WP-TotalPages": "13", "X-WP-Total": "1201"},
    ))
    assert [item["id"] for item in items] == list(range(1, 1202))
    assert len(requests) == 13


@pytest.mark.asyncio
async def test_headerless_full_batch_continues_until_a_short_batch(collection):
    items, requests = await collect(collection, lambda page: (batch(page, 3 if page == 3 else 100), {}))
    assert [item["id"] for item in items] == list(range(1, 204))
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_headerless_full_final_batch_cannot_claim_completion(collection):
    with pytest.raises(IncompleteInventory, match="pagination limit"):
        await collect(collection, lambda page: (batch(page), {}), max_pages=2)


@pytest.mark.asyncio
async def test_reported_page_limit_stops_before_partial_inventory_can_escape(collection):
    with pytest.raises(IncompleteInventory, match="pagination limit"):
        await collect(collection, lambda page: (batch(page), {"X-WP-TotalPages": "3"}), max_pages=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["broken", "-1", "1.5", "1e2", ""])
async def test_malformed_pagination_fails_visibly(collection, header):
    with pytest.raises(IncompleteInventory, match="pagination is inconsistent"):
        await collect(collection, lambda page: (batch(page, 1), {"X-WP-TotalPages": header}))


@pytest.mark.asyncio
async def test_empty_page_before_reported_end_is_incomplete(collection):
    with pytest.raises(IncompleteInventory):
        await collect(collection, lambda page: ([], {"X-WP-TotalPages": "2"}))


@pytest.mark.asyncio
async def test_empty_reported_last_page_does_not_claim_completion(collection):
    with pytest.raises(IncompleteInventory):
        await collect(collection, lambda page: (batch(page) if page == 1 else [], {"X-WP-TotalPages": "2"}))


@pytest.mark.asyncio
async def test_record_total_must_match_at_last_page(collection):
    with pytest.raises(IncompleteInventory):
        await collect(collection, lambda page: (batch(page, 1), {"X-WP-TotalPages": "1", "X-WP-Total": "2"}))


@pytest.mark.asyncio
async def test_changing_totals_cannot_mark_a_moving_collection_complete(collection):
    with pytest.raises(IncompleteInventory):
        await collect(collection, lambda page: (batch(page), {"X-WP-TotalPages": "2" if page == 1 else "1"}))


@pytest.mark.asyncio
async def test_repeated_batch_cannot_hide_missing_records(collection):
    with pytest.raises(IncompleteInventory, match="repeated records"):
        await collect(collection, lambda page: (batch(1), {"X-WP-TotalPages": "2"}))


@pytest.mark.asyncio
async def test_invalid_collection_item_cannot_be_silently_dropped(collection):
    with pytest.raises(IncompleteInventory, match="invalid or repeated records"):
        await collect(collection, lambda page: ([{"id": 1}, None], {"X-WP-TotalPages": "1"}))


@pytest.mark.asyncio
async def test_empty_collection_with_zero_totals_is_complete(collection):
    items, requests = await collect(collection, lambda page: ([], {"X-WP-TotalPages": "0", "X-WP-Total": "0"}))
    assert items == []
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_total_count_can_prove_a_full_last_batch_without_page_count(collection):
    items, requests = await collect(collection, lambda page: (batch(page), {"X-WP-Total": "200"}), max_pages=2)
    assert len(items) == 200
    assert len(requests) == 2
