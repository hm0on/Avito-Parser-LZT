"""VK parser tests — mocked VK API v5.199."""

import re

import pytest
import respx
from httpx import Response

from src.enrichment.review_parsers.vk import VK_API_BASE, VkParser
from tests.conftest import load_json_fixture


@pytest.fixture
def parser() -> VkParser:
    return VkParser()


@pytest.fixture
def groups_data():
    return load_json_fixture("vk_groups_getbyid.json")


@pytest.fixture
def wall_data():
    return load_json_fixture("vk_wall_get.json")


@pytest.fixture(autouse=True)
def set_vk_token(monkeypatch):
    monkeypatch.setattr("src.enrichment.review_parsers.vk.settings.vk_access_token", "FAKE_TOKEN")


# ---------------------------------------------------------------------------
# can_handle / guards
# ---------------------------------------------------------------------------


def test_can_handle_vk_com(parser):
    assert parser.can_handle("https://vk.com/burpro_omsk")


def test_can_handle_m_vk_com(parser):
    assert parser.can_handle("https://m.vk.com/burpro_omsk")


def test_cannot_handle_non_vk(parser):
    assert not parser.can_handle("https://flamp.ru/firm/burpro-123")


async def test_skip_when_no_token(parser, monkeypatch):
    monkeypatch.setattr("src.enrichment.review_parsers.vk.settings.vk_access_token", "")
    reviews = await parser.parse("https://vk.com/burpro_omsk", "БурПро")
    assert reviews == []


async def test_skip_wall_post_url(parser):
    """Individual wall post URLs (/wall-123_456) should be skipped."""
    reviews = await parser.parse("https://vk.com/wall-123456789_42", "БурПро")
    assert reviews == []


# ---------------------------------------------------------------------------
# _extract_screen_name
# ---------------------------------------------------------------------------


def test_extract_screen_name_standard(parser):
    assert parser._extract_screen_name("https://vk.com/burpro_omsk") == "burpro_omsk"


def test_extract_screen_name_numeric_returns_none(parser):
    """Numeric-only paths are user IDs, not group screen names."""
    assert parser._extract_screen_name("https://vk.com/123456") is None


def test_extract_screen_name_with_trailing_slash(parser):
    assert parser._extract_screen_name("https://vk.com/burpro_omsk/") == "burpro_omsk"


# ---------------------------------------------------------------------------
# Full parse — groups.getById + wall.get
# ---------------------------------------------------------------------------


@respx.mock
async def test_parse_returns_reviews(parser, groups_data, wall_data):
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/groups.getById"))).mock(
        return_value=Response(200, json=groups_data)
    )
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/wall.get"))).mock(
        return_value=Response(200, json=wall_data)
    )

    reviews = await parser.parse("https://vk.com/burpro_omsk", "БурПро")

    # wall fixture has 3 items but one has empty text → 2 reviews expected
    assert len(reviews) == 2
    assert reviews[0].source == "vk"
    assert reviews[0].rating is None  # wall posts have no star rating
    assert "Пробурили скважину" in reviews[0].text


@respx.mock
async def test_parse_review_date_converted(parser, groups_data, wall_data):
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/groups.getById"))).mock(
        return_value=Response(200, json=groups_data)
    )
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/wall.get"))).mock(
        return_value=Response(200, json=wall_data)
    )

    reviews = await parser.parse("https://vk.com/burpro_omsk", "БурПро")

    # date should be populated (UNIX timestamp → datetime)
    assert reviews[0].review_date is not None


@respx.mock
async def test_parse_skips_empty_text_posts(parser, groups_data, wall_data):
    """Wall posts with empty text should be excluded."""
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/groups.getById"))).mock(
        return_value=Response(200, json=groups_data)
    )
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/wall.get"))).mock(
        return_value=Response(200, json=wall_data)
    )

    reviews = await parser.parse("https://vk.com/burpro_omsk", "БурПро")
    # All returned reviews should have non-empty text
    for rv in reviews:
        assert rv.text


@respx.mock
async def test_parse_api_error_in_200_response(parser, groups_data):
    """VK returns HTTP 200 with {"error": ...} on failures — must return []."""
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/groups.getById"))).mock(
        return_value=Response(200, json=groups_data)
    )
    # wall.get returns HTTP 200 but with an error payload
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/wall.get"))).mock(
        return_value=Response(200, json={"error": {"error_code": 15, "error_msg": "Access denied"}})
    )

    reviews = await parser.parse("https://vk.com/burpro_omsk", "БурПро")
    assert reviews == []


@respx.mock
async def test_parse_groups_api_error(parser):
    """If groups.getById returns an error payload, parser should return []."""
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/groups.getById"))).mock(
        return_value=Response(200, json={"error": {"error_code": 100, "error_msg": "Not found"}})
    )

    reviews = await parser.parse("https://vk.com/unknown_group", "Неизвестная компания")
    assert reviews == []


@respx.mock
async def test_parse_group_not_found(parser):
    """If groups.getById returns empty groups list, parser should return []."""
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/groups.getById"))).mock(
        return_value=Response(200, json={"response": {"groups": []}})
    )

    reviews = await parser.parse("https://vk.com/nonexistent", "Test")
    assert reviews == []


# ---------------------------------------------------------------------------
# source_link format
# ---------------------------------------------------------------------------


@respx.mock
async def test_parse_source_link_format(parser, groups_data, wall_data):
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/groups.getById"))).mock(
        return_value=Response(200, json=groups_data)
    )
    respx.get(re.compile(re.escape(f"{VK_API_BASE}/wall.get"))).mock(
        return_value=Response(200, json=wall_data)
    )

    reviews = await parser.parse("https://vk.com/burpro_omsk", "БурПро")
    group_id = 123456789
    for rv in reviews:
        assert rv.source_link.startswith(f"https://vk.com/wall-{group_id}_")
