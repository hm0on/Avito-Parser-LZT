"""2GIS collector tests — respx mocks for Catalog API and Reviews API.

The new TwoGisCollector captures its API key at runtime via Playwright.
Tests bypass that by injecting a fake key directly into the class cache
(`TwoGisCollector._api_key`) and mocking httpx calls with respx.
"""

import pytest
import respx
from httpx import Response

from src.collectors.twogis import CATALOG_URL, REVIEWS_URL, TwoGisCollector
from tests.conftest import load_json_fixture


@pytest.fixture
def catalog_data():
    return load_json_fixture("twogis_catalog_response.json")


@pytest.fixture
def reviews_data():
    return load_json_fixture("twogis_reviews_response.json")


@pytest.fixture(autouse=True)
def inject_fake_api_key():
    """Inject a fake key so _ensure_api_key() returns immediately without Playwright."""
    TwoGisCollector._api_key = "FAKE_KEY"
    yield
    TwoGisCollector._api_key = None


# ---------------------------------------------------------------------------
# collect() guard — when key capture fails, returns []
# ---------------------------------------------------------------------------


async def test_collect_no_api_key(monkeypatch):
    """If _ensure_api_key() returns None (e.g. Playwright failed), collect returns []."""
    TwoGisCollector._api_key = None

    async def _no_key(self):
        return None

    monkeypatch.setattr(TwoGisCollector, "_ensure_api_key", _no_key)
    collector = TwoGisCollector()
    result = await collector.collect("бурение")
    assert result == []


# ---------------------------------------------------------------------------
# Basic collection
# ---------------------------------------------------------------------------


async def test_collect_basic(catalog_data, reviews_data):
    """Collector should parse both companies from the catalog response."""
    async with respx.mock as mock:
        mock.get(CATALOG_URL).mock(return_value=Response(200, json=catalog_data))
        for branch_id in ["141265769530959", "141265769530960"]:
            mock.get(REVIEWS_URL.format(branch_id=branch_id)).mock(
                return_value=Response(200, json=reviews_data)
            )

        collector = TwoGisCollector()
        companies = await collector.collect("бурение скважин")

    assert len(companies) == 2
    names = {c.name_raw for c in companies}
    assert "БурПро" in names
    assert "ИП Петров Сантехника" in names


async def test_collect_phones_extracted(catalog_data, reviews_data):
    async with respx.mock as mock:
        mock.get(CATALOG_URL).mock(return_value=Response(200, json=catalog_data))
        for branch_id in ["141265769530959", "141265769530960"]:
            mock.get(REVIEWS_URL.format(branch_id=branch_id)).mock(
                return_value=Response(200, json=reviews_data)
            )

        collector = TwoGisCollector()
        companies = await collector.collect("бурение")

    burpro = next(c for c in companies if c.name_raw == "БурПро")
    assert len(burpro.phones) == 2
    assert "+7 (913) 123-45-67" in burpro.phones


async def test_collect_ratings_from_catalog(catalog_data, reviews_data):
    async with respx.mock as mock:
        mock.get(CATALOG_URL).mock(return_value=Response(200, json=catalog_data))
        for branch_id in ["141265769530959", "141265769530960"]:
            mock.get(REVIEWS_URL.format(branch_id=branch_id)).mock(
                return_value=Response(200, json=reviews_data)
            )

        collector = TwoGisCollector()
        companies = await collector.collect("бурение")

    burpro = next(c for c in companies if c.name_raw == "БурПро")
    assert burpro.average_rating is not None


async def test_collect_reviews_fetched(catalog_data, reviews_data):
    async with respx.mock as mock:
        mock.get(CATALOG_URL).mock(return_value=Response(200, json=catalog_data))
        for branch_id in ["141265769530959", "141265769530960"]:
            mock.get(REVIEWS_URL.format(branch_id=branch_id)).mock(
                return_value=Response(200, json=reviews_data)
            )

        collector = TwoGisCollector()
        companies = await collector.collect("бурение")

    burpro = next(c for c in companies if c.name_raw == "БурПро")
    assert len(burpro.reviews) == 2
    texts = {r.text for r in burpro.reviews}
    assert any("доволен" in t for t in texts)


async def test_collect_reviews_404_skipped(catalog_data):
    """If reviews endpoint returns 404, company is still included without reviews."""
    async with respx.mock as mock:
        mock.get(CATALOG_URL).mock(return_value=Response(200, json=catalog_data))
        for branch_id in ["141265769530959", "141265769530960"]:
            mock.get(REVIEWS_URL.format(branch_id=branch_id)).mock(
                return_value=Response(404, json={})
            )

        collector = TwoGisCollector()
        companies = await collector.collect("бурение")

    assert len(companies) == 2
    for company in companies:
        assert company.reviews == []


# ---------------------------------------------------------------------------
# Expired key → cache cleared
# ---------------------------------------------------------------------------


async def test_collect_key_expired_clears_cache():
    """If catalog returns 401, the cached key should be cleared."""
    async with respx.mock as mock:
        mock.get(CATALOG_URL).mock(return_value=Response(401, json={"error": "unauthorized"}))

        collector = TwoGisCollector()
        companies = await collector.collect("бурение")

    assert companies == []
    assert TwoGisCollector._api_key is None  # key was cleared


# ---------------------------------------------------------------------------
# Pagination stops when no more items
# ---------------------------------------------------------------------------


async def test_collect_stops_on_empty_page(reviews_data):
    """Second page returns empty items → collector stops."""
    page1 = {
        "result": {
            "total": 1,
            "items": [
                {
                    "id": "aaa111",
                    "name_ex": {"primary": "Тест"},
                    "address": {"name": "ул. Теста, 1"},
                    "contact_groups": [],
                    "reviews": {"rating": 4.0, "count": 1},
                    "rubrics": [],
                    "point": {},
                }
            ],
        }
    }
    async with respx.mock as mock:
        mock.get(CATALOG_URL).mock(return_value=Response(200, json=page1))
        mock.get(REVIEWS_URL.format(branch_id="aaa111")).mock(
            return_value=Response(200, json=reviews_data)
        )

        collector = TwoGisCollector()
        companies = await collector.collect("тест")

    assert len(companies) == 1


# ---------------------------------------------------------------------------
# _parse_item edge cases
# ---------------------------------------------------------------------------


def test_parse_item_no_name():
    collector = TwoGisCollector()
    result = collector._parse_item({"id": "x"}, "kw")
    assert result is None


def test_parse_item_fallback_name():
    collector = TwoGisCollector()
    item = {
        "id": "x",
        "name": "Fallback Name",
        "contact_groups": [],
        "address": {},
        "reviews": {},
        "rubrics": [],
        "point": {},
    }
    result = collector._parse_item(item, "kw")
    assert result is not None
    assert result.name_raw == "Fallback Name"
