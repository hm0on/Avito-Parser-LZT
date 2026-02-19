"""Flamp parser tests — JSON API primary path + HTML scrape fallback."""

import re

import pytest
import respx
from httpx import Response

from src.enrichment.review_parsers.flamp import REVIEWS_API, FlampParser
from tests.conftest import load_fixture, load_json_fixture


@pytest.fixture
def parser() -> FlampParser:
    return FlampParser()


@pytest.fixture
def api_data():
    return load_json_fixture("flamp_api_response.json")


@pytest.fixture
def page_html():
    return load_fixture("flamp_page.html")


# ---------------------------------------------------------------------------
# can_handle / _extract_filial_id
# ---------------------------------------------------------------------------


def test_can_handle_flamp_url(parser):
    assert parser.can_handle("https://omsk.flamp.ru/firm/burpro-12345678")


def test_cannot_handle_other_domain(parser):
    assert not parser.can_handle("https://2gis.ru/omsk/firms/123")


def test_extract_filial_id_standard(parser):
    assert parser._extract_filial_id("https://omsk.flamp.ru/firm/burpro-12345678") == "12345678"


def test_extract_filial_id_trailing_slash(parser):
    assert parser._extract_filial_id("https://omsk.flamp.ru/firm/company-9999/") == "9999"


def test_extract_filial_id_none_when_no_digits(parser):
    assert parser._extract_filial_id("https://omsk.flamp.ru/firm/burpro") is None


# ---------------------------------------------------------------------------
# JSON API — primary path
# ---------------------------------------------------------------------------


async def test_parse_via_api_returns_reviews(parser, api_data):
    filial_id = "12345678"
    url = f"https://omsk.flamp.ru/firm/burpro-{filial_id}"
    api_url = REVIEWS_API.format(filial_id=filial_id)

    async with respx.mock:
        respx.get(re.compile(re.escape(api_url))).mock(return_value=Response(200, json=api_data))
        reviews = await parser.parse(url, "БурПро")

    assert len(reviews) == 3
    assert reviews[0].source == "flamp"
    assert reviews[0].rating == 5.0
    assert reviews[0].author == "Олег Сидоров"
    assert "Прекрасная компания" in reviews[0].text


async def test_parse_via_api_review_date_parsed(parser, api_data):
    filial_id = "12345678"
    url = f"https://omsk.flamp.ru/firm/burpro-{filial_id}"
    api_url = REVIEWS_API.format(filial_id=filial_id)

    async with respx.mock:
        respx.get(re.compile(re.escape(api_url))).mock(return_value=Response(200, json=api_data))
        reviews = await parser.parse(url, "БурПро")

    assert reviews[0].review_date is not None
    assert reviews[0].review_date.year == 2025
    assert reviews[0].review_date.month == 11


# ---------------------------------------------------------------------------
# 403 fallback to HTML scrape
# ---------------------------------------------------------------------------


async def test_parse_falls_back_on_403(parser, page_html):
    filial_id = "12345678"
    url = f"https://omsk.flamp.ru/firm/burpro-{filial_id}"
    api_url = REVIEWS_API.format(filial_id=filial_id)

    async with respx.mock:
        # Primary JSON API returns 403
        respx.get(re.compile(re.escape(api_url))).mock(return_value=Response(403))
        # Fallback HTML scrape
        respx.get(url).mock(return_value=Response(200, text=page_html))
        reviews = await parser.parse(url, "БурПро")

    assert len(reviews) >= 1
    assert reviews[0].source == "flamp"
    assert "Превосходный сервис" in reviews[0].text


async def test_parse_returns_empty_on_no_filial_id(parser):
    url = "https://omsk.flamp.ru/firm/burpro"  # no numeric ID
    reviews = await parser.parse(url, "БурПро")
    assert reviews == []


# ---------------------------------------------------------------------------
# HTML _parse_page (unit)
# ---------------------------------------------------------------------------


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
    for rv in reviews:
        assert rv.source_link == url
