"""2GIS collector tests — respx mocks for Catalog API and Reviews API."""

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
def set_twogis_key(monkeypatch):
    """Ensure a fake API key is set so collect() doesn't bail out early."""
    monkeypatch.setattr("src.collectors.twogis.settings.twogis_api_key", "FAKE_KEY")
    monkeypatch.setattr("src.collectors.twogis.settings.twogis_region_id", "4504222397119399")


# ---------------------------------------------------------------------------
# collect() guard
# ---------------------------------------------------------------------------


async def test_collect_no_api_key(monkeypatch):
    monkeypatch.setattr("src.collectors.twogis.settings.twogis_api_key", "")
    collector = TwoGisCollector()
    result = await collector.collect("бурение")
    assert result == []


# ---------------------------------------------------------------------------
# Basic collection
# ---------------------------------------------------------------------------


@respx.mock
async def test_collect_basic(catalog_data, reviews_data, monkeypatch):
    """Collector should parse both companies from the catalog response."""
    # Stub catalog endpoint (matches any page parameter)
    respx.get(CATALOG_URL).mock(return_value=Response(200, json=catalog_data))

    # Stub reviews endpoint for both branch IDs
    for branch_id in ["141265769530959", "141265769530960"]:
        respx.get(REVIEWS_URL.format(branch_id=branch_id)).mock(
            return_value=Response(200, json=reviews_data)
        )

    collector = TwoGisCollector()
    companies = await collector.collect("бурение скважин")

    assert len(companies) == 2
    names = {c.name_raw for c in companies}
    assert "БурПро" in names
    assert "ИП Петров Сантехника" in names


@respx.mock
async def test_collect_phones_extracted(catalog_data, reviews_data):
    respx.get(CATALOG_URL).mock(return_value=Response(200, json=catalog_data))
    for branch_id in ["141265769530959", "141265769530960"]:
        respx.get(REVIEWS_URL.format(branch_id=branch_id)).mock(
            return_value=Response(200, json=reviews_data)
        )

    collector = TwoGisCollector()
    companies = await collector.collect("бурение")

    burpro = next(c for c in companies if c.name_raw == "БурПро")
    assert len(burpro.phones) == 2
    assert "+7 (913) 123-45-67" in burpro.phones


@respx.mock
async def test_collect_ratings_from_catalog(catalog_data, reviews_data):
    respx.get(CATALOG_URL).mock(return_value=Response(200, json=catalog_data))
    for branch_id in ["141265769530959", "141265769530960"]:
        respx.get(REVIEWS_URL.format(branch_id=branch_id)).mock(
            return_value=Response(200, json=reviews_data)
        )

    collector = TwoGisCollector()
    companies = await collector.collect("бурение")

    burpro = next(c for c in companies if c.name_raw == "БурПро")
    # Reviews from the reviews endpoint override the catalog rating
    assert burpro.average_rating is not None


@respx.mock
async def test_collect_reviews_fetched(catalog_data, reviews_data):
    respx.get(CATALOG_URL).mock(return_value=Response(200, json=catalog_data))
    for branch_id in ["141265769530959", "141265769530960"]:
        respx.get(REVIEWS_URL.format(branch_id=branch_id)).mock(
            return_value=Response(200, json=reviews_data)
        )

    collector = TwoGisCollector()
    companies = await collector.collect("бурение")

    burpro = next(c for c in companies if c.name_raw == "БурПро")
    assert len(burpro.reviews) == 2
    texts = {r.text for r in burpro.reviews}
    assert any("доволен" in t for t in texts)


@respx.mock
async def test_collect_reviews_404_skipped(catalog_data):
    """If reviews endpoint returns 404, company is still included without reviews."""
    respx.get(CATALOG_URL).mock(return_value=Response(200, json=catalog_data))
    for branch_id in ["141265769530959", "141265769530960"]:
        respx.get(REVIEWS_URL.format(branch_id=branch_id)).mock(
            return_value=Response(404, json={})
        )

    collector = TwoGisCollector()
    companies = await collector.collect("бурение")

    assert len(companies) == 2
    for company in companies:
        assert company.reviews == []


# ---------------------------------------------------------------------------
# Pagination stops when no more items
# ---------------------------------------------------------------------------


@respx.mock
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
    respx.get(CATALOG_URL).mock(return_value=Response(200, json=page1))
    respx.get(REVIEWS_URL.format(branch_id="aaa111")).mock(
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
