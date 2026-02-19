"""Otzovik parser tests — HTML scrape with Cloudflare 403 handling."""

from datetime import datetime

import pytest
import respx
from httpx import Response

from src.enrichment.review_parsers.otzovik import OtzovikParser, _parse_ru_date
from tests.conftest import load_fixture


@pytest.fixture
def parser() -> OtzovikParser:
    return OtzovikParser()


@pytest.fixture
def page_html():
    return load_fixture("otzovik_page.html")


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


def test_can_handle_otzovik(parser):
    assert parser.can_handle("https://otzovik.com/reviews/burenie-skvazhin_omsk/")


def test_cannot_handle_other(parser):
    assert not parser.can_handle("https://flamp.ru/firm/burpro-123")


# ---------------------------------------------------------------------------
# _parse_ru_date
# ---------------------------------------------------------------------------


def test_parse_ru_date_full_month():
    dt = _parse_ru_date("19 февраля 2026")
    assert dt == datetime(2026, 2, 19)


def test_parse_ru_date_abbreviated():
    dt = _parse_ru_date("10 янв. 2026")
    assert dt == datetime(2026, 1, 10)


def test_parse_ru_date_december():
    dt = _parse_ru_date("31 декабря 2025")
    assert dt == datetime(2025, 12, 31)


def test_parse_ru_date_invalid_returns_none():
    assert _parse_ru_date("invalid date") is None
    assert _parse_ru_date("") is None
    assert _parse_ru_date("2026-02-19") is None  # not Russian format


def test_parse_ru_date_all_months():
    months = [
        ("января", 1), ("февраля", 2), ("марта", 3), ("апреля", 4),
        ("мая", 5), ("июня", 6), ("июля", 7), ("августа", 8),
        ("сентября", 9), ("октября", 10), ("ноября", 11), ("декабря", 12),
    ]
    for name, expected_month in months:
        dt = _parse_ru_date(f"1 {name} 2025")
        assert dt is not None, f"Failed to parse month: {name}"
        assert dt.month == expected_month


# ---------------------------------------------------------------------------
# Full parse — successful 200
# ---------------------------------------------------------------------------


@respx.mock
async def test_parse_basic(parser, page_html):
    url = "https://otzovik.com/reviews/burpro/"
    respx.get(url).mock(return_value=Response(200, text=page_html))

    reviews = await parser.parse(url, "БурПро")

    assert len(reviews) == 2
    assert reviews[0].source == "otzovik"
    assert "доволен качеством" in reviews[0].text
    assert reviews[0].rating == 5.0
    assert reviews[0].author == "ivanov_omsk"


@respx.mock
async def test_parse_dates_parsed(parser, page_html):
    url = "https://otzovik.com/reviews/burpro/"
    respx.get(url).mock(return_value=Response(200, text=page_html))

    reviews = await parser.parse(url, "БурПро")

    assert reviews[0].review_date == datetime(2026, 2, 19)
    assert reviews[1].review_date == datetime(2026, 1, 10)


@respx.mock
async def test_parse_source_link(parser, page_html):
    url = "https://otzovik.com/reviews/burpro/"
    respx.get(url).mock(return_value=Response(200, text=page_html))

    reviews = await parser.parse(url, "БурПро")

    for rv in reviews:
        assert rv.source_link == url


# ---------------------------------------------------------------------------
# Cloudflare 403 — soft failure
# ---------------------------------------------------------------------------


@respx.mock
async def test_parse_403_returns_empty(parser):
    url = "https://otzovik.com/reviews/burpro/"
    respx.get(url).mock(return_value=Response(403, text="Access denied"))

    reviews = await parser.parse(url, "БурПро")
    assert reviews == []


@respx.mock
async def test_parse_non_200_returns_empty(parser):
    url = "https://otzovik.com/reviews/burpro/"
    respx.get(url).mock(return_value=Response(500, text="Server Error"))

    reviews = await parser.parse(url, "БурПро")
    assert reviews == []


# ---------------------------------------------------------------------------
# _parse_page (unit — no HTTP)
# ---------------------------------------------------------------------------


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
