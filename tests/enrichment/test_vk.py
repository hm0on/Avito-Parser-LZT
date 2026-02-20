"""VK parser tests — Playwright-based web scraping (no VK API token needed).

Since VkParser now scrapes m.vk.com via Playwright, browser-dependent tests
mock the `_scrape()` method to avoid launching a real browser.
Pure-logic helpers (_extract_screen_name, can_handle) are tested without mocks.
"""

import pytest

from src.collectors.base import RawReview
from src.enrichment.review_parsers.vk import VkParser


@pytest.fixture
def parser() -> VkParser:
    return VkParser()


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


def test_can_handle_vk_com(parser):
    assert parser.can_handle("https://vk.com/burpro_omsk")


def test_can_handle_m_vk_com(parser):
    assert parser.can_handle("https://m.vk.com/burpro_omsk")


def test_cannot_handle_non_vk(parser):
    assert not parser.can_handle("https://flamp.ru/firm/burpro-123")


# ---------------------------------------------------------------------------
# Guards that skip scraping
# ---------------------------------------------------------------------------


async def test_skip_wall_post_url(parser):
    """Individual wall post URLs (/wall-123_456) should be skipped without scraping."""
    reviews = await parser.parse("https://vk.com/wall-123456789_42", "БурПро")
    assert reviews == []


async def test_skip_numeric_path(parser):
    """vk.com/12345 looks like a user profile, not a group — skip it."""
    reviews = await parser.parse("https://vk.com/123456", "Test")
    assert reviews == []


# ---------------------------------------------------------------------------
# _extract_screen_name
# ---------------------------------------------------------------------------


def test_extract_screen_name_standard(parser):
    assert parser._extract_screen_name("https://vk.com/burpro_omsk") == "burpro_omsk"


def test_extract_screen_name_numeric_returns_none(parser):
    assert parser._extract_screen_name("https://vk.com/123456") is None


def test_extract_screen_name_with_trailing_slash(parser):
    # The regex stops at '/', so burpro_omsk is captured before the slash
    result = parser._extract_screen_name("https://vk.com/burpro_omsk/")
    assert result == "burpro_omsk"


def test_extract_screen_name_with_query(parser):
    result = parser._extract_screen_name("https://vk.com/burpro_omsk?w=wall-1_2")
    assert result == "burpro_omsk"


# ---------------------------------------------------------------------------
# Full parse — _scrape() mocked to avoid real browser
# ---------------------------------------------------------------------------


async def test_parse_returns_reviews(parser, monkeypatch):
    """parse() should return whatever _scrape() yields."""
    fake_reviews = [
        RawReview(
            source="vk",
            text="Пробурили скважину за 2 дня, всё отлично!",
            rating=None,
            author=None,
            review_date=None,
            source_link="https://vk.com/burpro_omsk",
        ),
        RawReview(
            source="vk",
            text="Работа выполнена качественно",
            rating=None,
            author=None,
            review_date=None,
            source_link="https://vk.com/burpro_omsk",
        ),
    ]

    async def fake_scrape(screen_name):
        return fake_reviews

    monkeypatch.setattr(parser, "_scrape", fake_scrape)

    reviews = await parser.parse("https://vk.com/burpro_omsk", "БурПро")
    assert len(reviews) == 2
    assert reviews[0].source == "vk"
    assert reviews[0].rating is None
    assert "Пробурили скважину" in reviews[0].text


async def test_parse_returns_empty_on_scrape_error(parser, monkeypatch):
    """If _scrape() raises, parse() should safely return []."""

    async def failing_scrape(screen_name):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(parser, "_scrape", failing_scrape)

    reviews = await parser.parse("https://vk.com/burpro_omsk", "БурПро")
    assert reviews == []


async def test_parse_empty_group(parser, monkeypatch):
    """If _scrape() returns [], parse() returns []."""

    async def empty_scrape(screen_name):
        return []

    monkeypatch.setattr(parser, "_scrape", empty_scrape)

    reviews = await parser.parse("https://vk.com/empty_group", "Пустая компания")
    assert reviews == []


async def test_parse_source_link_format(parser, monkeypatch):
    """Reviews source_link should point to the group page."""
    expected_link = "https://vk.com/burpro_omsk"
    fake_review = RawReview(
        source="vk",
        text="Хорошая работа",
        rating=None,
        author=None,
        review_date=None,
        source_link=expected_link,
    )

    async def fake_scrape(screen_name):
        return [fake_review]

    monkeypatch.setattr(parser, "_scrape", fake_scrape)

    reviews = await parser.parse("https://vk.com/burpro_omsk", "БурПро")
    assert reviews[0].source_link == expected_link
