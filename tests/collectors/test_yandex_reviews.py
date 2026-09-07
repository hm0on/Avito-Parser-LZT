"""Tests for the Yandex collector review fallback behavior."""

import pytest

from src.collectors.base import RawCompany, RawReview
from src.collectors.yandex import YandexCollector


@pytest.fixture
def collector():
    return YandexCollector()


# ── _should_open_reviews: relaxed gating ──────────────────────────────────


def test_should_open_reviews_with_count(collector):
    """Open reviews when reviews_count > 0."""
    company = RawCompany(source="yandex", reviews_count=5, reviews=[])
    assert collector._should_open_reviews(company) is True


def test_should_open_reviews_with_source_link_no_count(collector):
    """Open reviews even when reviews_count is None if source_link exists."""
    company = RawCompany(
        source="yandex",
        reviews_count=None,
        reviews=[],
        source_link="https://yandex.ru/maps/org/12345/",
    )
    assert collector._should_open_reviews(company) is True


def test_should_not_open_reviews_no_link_no_count(collector):
    """Don't open reviews when both reviews_count is None and no source_link."""
    company = RawCompany(source="yandex", reviews_count=None, reviews=[])
    assert collector._should_open_reviews(company) is False


def test_should_not_open_reviews_already_have(collector):
    """Don't open reviews if already collected."""
    company = RawCompany(
        source="yandex",
        reviews_count=5,
        reviews=[RawReview(source="yandex", text="Test")],
    )
    assert collector._should_open_reviews(company) is False


def test_should_open_reviews_count_zero(collector):
    """Don't open reviews when reviews_count is explicitly 0 and no source_link."""
    company = RawCompany(source="yandex", reviews_count=0, reviews=[])
    assert collector._should_open_reviews(company) is False


def test_should_open_reviews_count_zero_with_link(collector):
    """Open reviews when reviews_count=0 but source_link exists (might have reviews)."""
    company = RawCompany(
        source="yandex",
        reviews_count=0,
        reviews=[],
        source_link="https://yandex.ru/maps/org/12345/",
    )
    assert collector._should_open_reviews(company) is True


# ── _build_reviews_url ────────────────────────────────────────────────────


def test_build_reviews_url_from_source_link(collector):
    company = RawCompany(
        source="yandex",
        source_link="https://yandex.ru/maps/org/12345",
    )
    url = collector._build_reviews_url(company)
    assert url == "https://yandex.ru/maps/org/12345/reviews/"


def test_build_reviews_url_from_source_link_with_reviews(collector):
    company = RawCompany(
        source="yandex",
        source_link="https://yandex.ru/maps/org/12345/reviews/",
    )
    url = collector._build_reviews_url(company)
    assert "/reviews" in url


def test_build_reviews_url_from_source_id(collector):
    company = RawCompany(source="yandex", source_id="12345")
    url = collector._build_reviews_url(company)
    assert url == "https://yandex.ru/maps/org/12345/reviews/"


def test_build_reviews_url_none_when_no_data(collector):
    company = RawCompany(source="yandex")
    url = collector._build_reviews_url(company)
    assert url is None


# ── Review deduplication ──────────────────────────────────────────────────


def test_dedupe_reviews_removes_duplicates(collector):
    reviews = [
        RawReview(source="yandex", text="Good service", author="User1", rating=5.0),
        RawReview(source="yandex", text="Good service", author="User1", rating=5.0),
        RawReview(source="yandex", text="Bad service", author="User2", rating=1.0),
    ]
    deduped = collector._dedupe_reviews(reviews)
    assert len(deduped) == 2
