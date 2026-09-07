"""Tests for the 2GIS collector's Flamp fallback behavior."""

from unittest.mock import AsyncMock, patch

import pytest

from src.collectors.base import RawCompany, RawReview
from src.collectors.twogis import TwoGisCollector


@pytest.fixture
def collector():
    return TwoGisCollector()


# ── Flamp fallback ────────────────────────────────────────────────────────


async def test_flamp_fallback_returns_reviews_tagged_as_2gis(collector):
    """When Flamp fallback succeeds, reviews should be tagged as 2gis source."""
    flamp_reviews = [
        RawReview(source="flamp", text="Great place", rating=5.0, author="User1"),
        RawReview(source="flamp", text="OK service", rating=3.0, author="User2"),
    ]

    mock_parser = AsyncMock()
    mock_parser.safe_parse = AsyncMock(return_value=flamp_reviews)

    with patch("src.collectors.twogis._get_flamp_parser", return_value=mock_parser):
        result = await collector._fetch_flamp_fallback("12345", "Test Company")

    assert len(result) == 2
    # Reviews should be re-tagged as 2gis
    assert all(r.source == "2gis" for r in result)


async def test_flamp_fallback_returns_empty_on_error(collector):
    """When Flamp fallback fails, it should return empty list."""
    mock_parser = AsyncMock()
    mock_parser.safe_parse = AsyncMock(side_effect=Exception("Network error"))

    with patch("src.collectors.twogis._get_flamp_parser", return_value=mock_parser):
        result = await collector._fetch_flamp_fallback("12345", "Test Company")

    assert result == []


async def test_flamp_fallback_returns_empty_when_no_reviews(collector):
    """When Flamp finds no reviews, return empty list."""
    mock_parser = AsyncMock()
    mock_parser.safe_parse = AsyncMock(return_value=[])

    with patch("src.collectors.twogis._get_flamp_parser", return_value=mock_parser):
        result = await collector._fetch_flamp_fallback("12345", "Test Company")

    assert result == []


async def test_flamp_fallback_calls_with_correct_url(collector):
    """Flamp fallback should construct the correct flamp URL from source_id."""
    mock_parser = AsyncMock()
    mock_parser.safe_parse = AsyncMock(return_value=[])

    with patch("src.collectors.twogis._get_flamp_parser", return_value=mock_parser):
        await collector._fetch_flamp_fallback("70000001012345", "Test Company")

    mock_parser.safe_parse.assert_awaited_once_with(
        "https://omsk.flamp.ru/firm/70000001012345",
        "Test Company",
    )
