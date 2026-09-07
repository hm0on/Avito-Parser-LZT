"""Tests for native 2GIS reviews API extraction in twogis collector."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.collectors.base import RawReview
from src.collectors.twogis import (
    TwoGisCollector,
    _extract_review_api_key,
)


# ── reviewApiKey extraction ──────────────────────────────────────────────


class TestExtractReviewApiKey:
    def test_extracts_key_from_double_quoted_json(self):
        html = '{"reviewApiKey":"abc123def456ghij7890"}'
        assert _extract_review_api_key(html) == "abc123def456ghij7890"

    def test_extracts_key_from_script_context(self):
        html = """
        <script>
          window.__CONFIG__ = {
            "reviewApiKey": "zzzzyyyyxxxxwwwwvvvv"
          };
        </script>
        """
        assert _extract_review_api_key(html) == "zzzzyyyyxxxxwwwwvvvv"

    def test_extracts_key_from_REVIEW_API_KEY_pattern(self):
        html = 'REVIEW_API_KEY="abcdefghij1234567890"'
        assert _extract_review_api_key(html) == "abcdefghij1234567890"

    def test_returns_none_when_no_key(self):
        html = "<html><body>Hello world</body></html>"
        assert _extract_review_api_key(html) is None

    def test_returns_none_for_empty_html(self):
        assert _extract_review_api_key("") is None
        assert _extract_review_api_key(None) is None

    def test_short_key_not_matched(self):
        html = '"reviewApiKey":"short"'
        assert _extract_review_api_key(html) is None


# ── Native API reviews fetch ─────────────────────────────────────────────


REVIEWS_API_RESPONSE = {
    "reviews": [
        {
            "text": "Отличная компания, сделали всё быстро и качественно!",
            "rating": 5,
            "author": {"name": "Иван Петров"},
            "date_created": "2025-11-15T10:30:00Z",
        },
        {
            "text": "Нормальный сервис, но долго ждали результат.",
            "rating": 3,
            "author": "Мария",
            "date_created": "2025-10-20T14:00:00Z",
        },
    ]
}

HTML_WITH_KEY = '<script>{"reviewApiKey":"testapikey12345678901234"}</script>'
HTML_WITHOUT_KEY = "<html><body>No key here</body></html>"


@pytest.fixture
def collector():
    return TwoGisCollector()


@pytest.fixture(autouse=True)
def mock_proxy_manager(monkeypatch):
    """Ensure proxy manager returns a dummy proxy."""
    import src.collectors.twogis as twogis_mod
    monkeypatch.setattr(twogis_mod.twogis_proxy_manager, "_proxies", ["http://127.0.0.1:8080"])
    monkeypatch.setattr(twogis_mod.twogis_proxy_manager, "_index", 0)


async def test_native_api_success(collector):
    """Native API returns reviews when key exists and API responds 200."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = REVIEWS_API_RESPONSE

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.collectors.twogis.httpx.AsyncClient", return_value=mock_client):
        reviews = await collector._fetch_native_api_reviews(
            "12345", HTML_WITH_KEY, source_link="https://2gis.ru/omsk/firm/12345",
        )

    assert len(reviews) == 2
    assert reviews[0].source == "2gis"
    assert reviews[0].rating == 5.0
    assert reviews[0].author == "Иван Петров"
    assert reviews[0].review_date is not None
    # String author handling
    assert reviews[1].author == "Мария"


async def test_native_api_no_key_returns_empty(collector):
    """When no reviewApiKey found, returns empty without making HTTP calls."""
    with patch("src.collectors.twogis.httpx.AsyncClient") as mock_cls:
        reviews = await collector._fetch_native_api_reviews(
            "12345", HTML_WITHOUT_KEY, source_link="https://2gis.ru/omsk/firm/12345",
        )

    assert reviews == []
    mock_cls.assert_not_called()


async def test_native_api_blocked_retries_and_returns_empty(collector):
    """When API returns 403, retries with another proxy and eventually returns empty."""
    mock_response = MagicMock()
    mock_response.status_code = 403

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.collectors.twogis.httpx.AsyncClient", return_value=mock_client):
        reviews = await collector._fetch_native_api_reviews(
            "12345", HTML_WITH_KEY, source_link="https://2gis.ru/omsk/firm/12345",
        )

    assert reviews == []


async def test_native_api_timeout_retries(collector):
    """Network timeout triggers retry with another proxy."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.collectors.twogis.httpx.AsyncClient", return_value=mock_client):
        reviews = await collector._fetch_native_api_reviews(
            "12345", HTML_WITH_KEY, source_link="https://2gis.ru/omsk/firm/12345",
        )

    assert reviews == []


async def test_native_api_deduplicates_reviews(collector):
    """Duplicate reviews are deduplicated by text prefix."""
    duped_response = {
        "reviews": [
            {"text": "Отличная компания, всё супер и здорово!", "rating": 5, "author": {"name": "A"}},
            {"text": "Отличная компания, всё супер и здорово!", "rating": 5, "author": {"name": "B"}},
            {"text": "Другой отзыв тоже достаточно длинный", "rating": 4, "author": {"name": "C"}},
        ]
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = duped_response

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.collectors.twogis.httpx.AsyncClient", return_value=mock_client):
        reviews = await collector._fetch_native_api_reviews(
            "12345", HTML_WITH_KEY, source_link="https://2gis.ru/omsk/firm/12345",
        )

    assert len(reviews) == 2


# ── End-to-end: _scrape_firm_page_status with review priority chain ──────


async def test_firm_page_uses_native_api_when_available(collector, monkeypatch):
    """When native API returns reviews, DOM and Flamp fallbacks are not used."""
    firm_html = """
    <html>
      <head>
        <script type="application/ld+json">
          {"aggregateRating": {"ratingValue": "4.2", "reviewCount": "15"}}
        </script>
        <script>{"reviewApiKey":"testapikey12345678901234"}</script>
      </head>
      <body>
        <h1>Test Company</h1>
        <a href="tel:+79131234567">Call</a>
      </body>
    </html>
    """

    native_reviews = [
        RawReview(source="2gis", text="Great service indeed!", rating=5.0, author="User1",
                  source_link="https://2gis.ru/omsk/firm/12345"),
        RawReview(source="2gis", text="Pretty good overall experience", rating=4.0, author="User2",
                  source_link="https://2gis.ru/omsk/firm/12345"),
    ]

    collector._fetch_native_api_reviews = AsyncMock(return_value=native_reviews)
    collector._fetch_flamp_fallback = AsyncMock(return_value=[])

    # Mock the page navigation
    mock_page = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_page.goto = AsyncMock(return_value=mock_resp)
    mock_page.url = "https://2gis.ru/omsk/firm/12345"
    mock_page.content = AsyncMock(return_value=firm_html)
    mock_page.close = AsyncMock()

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    import asyncio
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    company, retry = await collector._scrape_firm_page_status(
        mock_context, "https://2gis.ru/omsk/firm/12345", "test"
    )

    assert company is not None
    assert company.reviews_count == 2
    assert len(company.reviews) == 2
    assert company.raw_payload["review_source"] == "2gis_native_api"
    # Flamp fallback should NOT have been called
    collector._fetch_flamp_fallback.assert_not_awaited()


async def test_firm_page_falls_back_to_flamp(collector, monkeypatch):
    """When native API and DOM both fail, Flamp fallback is used."""
    firm_html = """
    <html>
      <head>
        <script type="application/ld+json">
          {"aggregateRating": {"ratingValue": "4.0", "reviewCount": "5"}}
        </script>
      </head>
      <body><h1>Test Company</h1></body>
    </html>
    """

    flamp_reviews = [
        RawReview(source="2gis", text="Flamp review text is long enough", rating=4.0, author="FlampUser"),
    ]

    collector._fetch_native_api_reviews = AsyncMock(return_value=[])
    collector._fetch_flamp_fallback = AsyncMock(return_value=flamp_reviews)

    mock_page = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_page.goto = AsyncMock(return_value=mock_resp)
    mock_page.url = "https://2gis.ru/omsk/firm/12345"
    mock_page.content = AsyncMock(return_value=firm_html)
    mock_page.close = AsyncMock()

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    import asyncio
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr("src.collectors.twogis.settings.twogis_flamp_fallback_enabled", True)

    company, retry = await collector._scrape_firm_page_status(
        mock_context, "https://2gis.ru/omsk/firm/12345", "test"
    )

    assert company is not None
    assert len(company.reviews) == 1
    assert company.raw_payload["review_source"] == "2gis_flamp_fallback"
    collector._fetch_flamp_fallback.assert_awaited_once()


async def test_firm_page_uses_initial_state_fallback(collector, monkeypatch):
    """When native API is empty, initialState reviews should be used before Flamp."""
    state = {
        "data": {
            "review": {
                "r1": {
                    "data": {
                        "text": "Хорошая компания, сделали монтаж быстро и качественно.",
                        "rating": 5,
                        "date_created": "2025-12-10T12:00:00+07:00",
                        "user": {"name": "Иван"},
                        "object": {"id": "12345"},
                    }
                }
            }
        }
    }
    escaped = json.dumps(state, ensure_ascii=True).replace("\\", "\\\\").replace("'", "\\'")
    firm_html = f"""
    <html>
      <body>
        <h1>Test Company</h1>
        <script>var initialState = JSON.parse('{escaped}');</script>
      </body>
    </html>
    """

    collector._fetch_native_api_reviews = AsyncMock(return_value=[])
    collector._fetch_flamp_fallback = AsyncMock(return_value=[])

    mock_page = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_page.goto = AsyncMock(return_value=mock_resp)
    mock_page.url = "https://2gis.ru/omsk/firm/12345"
    mock_page.content = AsyncMock(return_value=firm_html)
    mock_page.close = AsyncMock()

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    import asyncio
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr("src.collectors.twogis.settings.twogis_flamp_fallback_enabled", True)

    company, retry = await collector._scrape_firm_page_status(
        mock_context, "https://2gis.ru/omsk/firm/12345", "test"
    )

    assert company is not None
    assert len(company.reviews) == 1
    assert company.reviews_count == 1
    assert company.raw_payload["review_source"] == "2gis_initial_state"
    collector._fetch_flamp_fallback.assert_not_awaited()
