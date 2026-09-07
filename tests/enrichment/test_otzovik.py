"""Otzovik parser tests."""

from __future__ import annotations

from datetime import datetime

import pytest
import httpx

import src.enrichment.review_parsers.otzovik as otzovik_module
from src.enrichment.review_parsers.otzovik import OtzovikParser, _parse_ru_date
from src.proxy import enrichment_proxy_manager
from tests.conftest import load_fixture


@pytest.fixture
def parser() -> OtzovikParser:
    return OtzovikParser()


@pytest.fixture(autouse=True)
def proxy_pool(monkeypatch):
    monkeypatch.setattr(
        enrichment_proxy_manager,
        "_proxies",
        ["http://user:pass@127.0.0.1:8080"],
    )
    monkeypatch.setattr(otzovik_module, "iter_proxy_urls", lambda *args, **kwargs: iter(["http://proxy"]))


@pytest.fixture
def page_html():
    return load_fixture("otzovik_page.html")


def _install_client(monkeypatch, response: httpx.Response | Exception):
    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, follow_redirects=True):
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(otzovik_module.httpx, "AsyncClient", DummyClient)


def test_can_handle_otzovik(parser):
    assert parser.can_handle("https://otzovik.com/reviews/burenie-skvazhin_omsk/")


def test_cannot_handle_other(parser):
    assert not parser.can_handle("https://flamp.ru/firm/burpro-123")


def test_parse_ru_date_full_month():
    assert _parse_ru_date("19 февраля 2026") == datetime(2026, 2, 19)


def test_parse_ru_date_abbreviated():
    assert _parse_ru_date("10 янв. 2026") == datetime(2026, 1, 10)


def test_parse_ru_date_december():
    assert _parse_ru_date("31 декабря 2025") == datetime(2025, 12, 31)


def test_parse_ru_date_invalid_returns_none():
    assert _parse_ru_date("invalid date") is None
    assert _parse_ru_date("") is None
    assert _parse_ru_date("2026-02-19") is None


def test_parse_ru_date_all_months():
    months = [
        ("января", 1), ("февраля", 2), ("марта", 3), ("апреля", 4),
        ("мая", 5), ("июня", 6), ("июля", 7), ("августа", 8),
        ("сентября", 9), ("октября", 10), ("ноября", 11), ("декабря", 12),
    ]
    for name, expected_month in months:
        dt = _parse_ru_date(f"1 {name} 2025")
        assert dt is not None
        assert dt.month == expected_month


async def test_parse_basic(parser, page_html, monkeypatch):
    _install_client(monkeypatch, httpx.Response(200, text=page_html))

    reviews = await parser.parse("https://otzovik.com/reviews/burpro/", "БурПро")

    assert len(reviews) == 2
    assert reviews[0].source == "otzovik"
    assert "доволен качеством" in reviews[0].text
    assert reviews[0].rating == 5.0
    assert reviews[0].author == "ivanov_omsk"


async def test_parse_dates_parsed(parser, page_html, monkeypatch):
    _install_client(monkeypatch, httpx.Response(200, text=page_html))

    reviews = await parser.parse("https://otzovik.com/reviews/burpro/", "БурПро")

    assert reviews[0].review_date == datetime(2026, 2, 19)
    assert reviews[1].review_date == datetime(2026, 1, 10)


async def test_parse_source_link(parser, page_html, monkeypatch):
    url = "https://otzovik.com/reviews/burpro/"
    _install_client(monkeypatch, httpx.Response(200, text=page_html))

    reviews = await parser.parse(url, "БурПро")

    for review in reviews:
        assert review.source_link == url


async def test_parse_403_returns_empty(parser, monkeypatch):
    _install_client(monkeypatch, httpx.Response(403, text="Access denied"))
    assert await parser.parse("https://otzovik.com/reviews/burpro/", "БурПро") == []


async def test_parse_non_200_returns_empty(parser, monkeypatch):
    _install_client(monkeypatch, httpx.Response(500, text="Server Error"))
    assert await parser.parse("https://otzovik.com/reviews/burpro/", "БурПро") == []


def test_parse_page_returns_reviews(parser, page_html):
    reviews = parser._parse_page(page_html, "https://otzovik.com/reviews/burpro/")
    assert len(reviews) == 2


def test_parse_page_text_content(parser, page_html):
    reviews = parser._parse_page(page_html, "https://otzovik.com/reviews/burpro/")
    assert "доволен" in reviews[0].text
    assert "рекомендую" in reviews[1].text


def test_parse_page_empty_html(parser):
    reviews = parser._parse_page("<html><body></body></html>", "https://otzovik.com/reviews/x/")
    assert reviews == []
