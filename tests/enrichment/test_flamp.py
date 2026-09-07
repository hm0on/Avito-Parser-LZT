"""Flamp parser tests."""

from __future__ import annotations

import pytest
import httpx

import src.enrichment.review_parsers.flamp as flamp_module
from src.collectors.base import RawReview
from src.enrichment.review_parsers.flamp import FlampParser, normalize_flamp_url
from src.proxy import enrichment_proxy_manager
from tests.conftest import load_fixture


@pytest.fixture
def parser() -> FlampParser:
    return FlampParser()


@pytest.fixture(autouse=True)
def proxy_pool(monkeypatch):
    monkeypatch.setattr(
        enrichment_proxy_manager,
        "_proxies",
        ["http://user:pass@127.0.0.1:8080"],
    )
    monkeypatch.setattr(flamp_module, "iter_proxy_urls", lambda *args, **kwargs: iter(["http://proxy"]))


@pytest.fixture
def page_html():
    return load_fixture("flamp_page.html")


def _install_dummy_client(monkeypatch):
    class DummyAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(flamp_module.httpx, "AsyncClient", DummyAsyncClient)


def test_can_handle_flamp_url(parser):
    assert parser.can_handle("https://omsk.flamp.ru/firm/burpro-12345678")


def test_cannot_handle_other_domain(parser):
    assert not parser.can_handle("https://2gis.ru/omsk/firms/123")


def test_extract_filial_id_standard(parser):
    assert parser._extract_filial_id("https://omsk.flamp.ru/firm/burpro-12345678") == "12345678"


def test_extract_filial_id_trailing_slash(parser):
    assert parser._extract_filial_id("https://omsk.flamp.ru/firm/company-9999/") == "9999"


def test_extract_filial_id_plain_numeric_path(parser):
    assert parser._extract_filial_id("https://omsk.flamp.ru/firm/70000001079754915") == "70000001079754915"


def test_extract_filial_id_none_when_no_digits(parser):
    assert parser._extract_filial_id("https://omsk.flamp.ru/firm/burpro") is None


def test_normalize_flamp_url_scheme_relative():
    assert normalize_flamp_url("//omsk.flamp.ru/firm/burpro-12345678") == "https://omsk.flamp.ru/firm/burpro-12345678"


def test_normalize_flamp_url_host_relative():
    assert normalize_flamp_url("/firm/burpro-12345678") == "https://omsk.flamp.ru/firm/burpro-12345678"


def test_normalize_flamp_url_repairs_double_host():
    assert (
        normalize_flamp_url("https://omsk.flamp.ru//omsk.flamp.ru/firm/burpro-12345678")
        == "https://omsk.flamp.ru/firm/burpro-12345678"
    )


async def test_parse_via_api_returns_reviews(parser, monkeypatch):
    _install_dummy_client(monkeypatch)

    async def fake_fetch_via_api(self, filial_id, client, **kwargs):
        return [
            RawReview(
                source="flamp",
                text="Прекрасная компания, все сделали быстро.",
                rating=5.0,
                author="Олег Сидоров",
                review_date=httpx.Timestamp(0).datetime if False else None,
                source_link="https://omsk.flamp.ru/firm/12345678",
            ),
            RawReview(source="flamp", text="Хороший сервис.", rating=4.0, author="Анна"),
            RawReview(source="flamp", text="Сделали работу в срок.", rating=5.0, author="Иван"),
        ]

    monkeypatch.setattr(FlampParser, "_fetch_via_api", fake_fetch_via_api)

    reviews = await parser.parse("https://omsk.flamp.ru/firm/burpro-12345678", "БурПро")

    assert len(reviews) == 3
    assert reviews[0].source == "flamp"
    assert reviews[0].rating == 5.0
    assert reviews[0].author == "Олег Сидоров"
    assert "Прекрасная компания" in reviews[0].text


async def test_parse_via_api_review_date_parsed(parser, monkeypatch):
    from datetime import datetime

    _install_dummy_client(monkeypatch)

    async def fake_fetch_via_api(self, filial_id, client, **kwargs):
        return [
            RawReview(
                source="flamp",
                text="ok",
                rating=5.0,
                author="Олег Сидоров",
                review_date=datetime(2025, 11, 21),
                source_link="https://omsk.flamp.ru/firm/12345678",
            )
        ]

    monkeypatch.setattr(FlampParser, "_fetch_via_api", fake_fetch_via_api)

    reviews = await parser.parse("https://omsk.flamp.ru/firm/burpro-12345678", "БурПро")

    assert reviews[0].review_date is not None
    assert reviews[0].review_date.year == 2025
    assert reviews[0].review_date.month == 11


async def test_parse_falls_back_on_403(parser, page_html, monkeypatch):
    _install_dummy_client(monkeypatch)

    async def fake_fetch_via_api(self, filial_id, client, **kwargs):
        request = httpx.Request("GET", "https://omsk.flamp.ru/api/2.0/filials/12345678/reviews/")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("blocked", request=request, response=response)

    async def fake_fetch_via_scrape(self, url, client, **kwargs):
        return parser._parse_page(page_html, url)

    monkeypatch.setattr(FlampParser, "_fetch_via_api", fake_fetch_via_api)
    monkeypatch.setattr(FlampParser, "_fetch_via_scrape", fake_fetch_via_scrape)

    reviews = await parser.parse("https://omsk.flamp.ru/firm/burpro-12345678", "БурПро")

    assert len(reviews) >= 1
    assert reviews[0].source == "flamp"
    assert "Превосходный сервис" in reviews[0].text


async def test_parse_returns_empty_on_no_filial_id(parser):
    reviews = await parser.parse("https://omsk.flamp.ru/firm/burpro", "БурПро")
    assert reviews == []


async def test_parse_attempts_direct_when_proxy_pool_empty(parser, monkeypatch):
    _install_dummy_client(monkeypatch)
    monkeypatch.setattr(enrichment_proxy_manager, "_proxies", [])

    async def fake_fetch_via_api(self, filial_id, client, **kwargs):
        return [RawReview(source="flamp", text="ok", rating=5.0)]

    monkeypatch.setattr(FlampParser, "_fetch_via_api", fake_fetch_via_api)
    reviews = await parser.parse("https://omsk.flamp.ru/firm/burpro-12345678", "БурПро")
    assert len(reviews) == 1
    assert reviews[0].source == "flamp"


def test_parse_page_extracts_text_and_rating(parser, page_html):
    reviews = parser._parse_page(page_html, "https://omsk.flamp.ru/firm/burpro-123")

    assert len(reviews) == 2
    assert reviews[0].text == "Превосходный сервис! Рекомендую всем."
    assert reviews[0].rating == 5.0
    assert reviews[0].author == "Геннадий Орлов"
    assert reviews[0].review_date is not None


def test_parse_page_source_link_set(parser, page_html):
    url = "https://omsk.flamp.ru/firm/burpro-123"
    reviews = parser._parse_page(page_html, url)
    for review in reviews:
        assert review.source_link == url


def test_parse_page_falls_back_to_jsonld(parser):
    html = """
    <html><head>
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Organization",
        "review": [{
          "@type": "Review",
          "reviewBody": "Отлично сделали бурение.",
          "author": {"@type": "Person", "name": "Анна"},
          "datePublished": "2026-02-01",
          "reviewRating": {"@type": "Rating", "ratingValue": "5"}
        }]
      }
      </script>
    </head><body></body></html>
    """

    reviews = parser._parse_page(html, "https://omsk.flamp.ru/firm/burpro-123")
    assert len(reviews) == 1
    assert reviews[0].text == "Отлично сделали бурение."
    assert reviews[0].author == "Анна"
    assert reviews[0].rating == 5.0


@pytest.mark.asyncio
async def test_fetch_via_api_respects_max_reviews(parser):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def get(self, url, params):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(
                    {
                        "results": [
                            {"text": "r1", "rating": 5},
                            {"text": "r2", "rating": 4},
                        ]
                    }
                )
            return FakeResponse(
                {
                    "results": [
                        {"text": "r3", "rating": 3},
                        {"text": "r4", "rating": 5},
                    ]
                }
            )

    client = FakeClient()
    reviews = await parser._fetch_via_api("12345678", client, max_reviews=3)
    assert len(reviews) == 3
    assert [r.text for r in reviews] == ["r1", "r2", "r3"]
