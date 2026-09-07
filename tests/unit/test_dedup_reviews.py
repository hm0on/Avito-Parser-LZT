"""Tests for review count handling in deduplication and enrichment runner.

Ensures that:
1. Deduplicator sets reviews_count to None (factual count set later).
2. gather_reviews returns only actual review rows.
3. enrichment_runner sets reviews_count = len(factual gathered reviews).
"""

import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.collectors.base import RawReview
from src.deduplication.deduplicator import CanonicalCard, Deduplicator
from src.pipeline.common import gather_reviews


# ── Deduplicator: reviews_count is None after merge ──────────────────────


def _make_raw_enriched_pair(
    *,
    name: str = "Test Company",
    inn: str | None = None,
    source: str = "yandex",
    reviews_count: int | None = 10,
    average_rating: float | None = 4.5,
    confidence: int = 80,
):
    """Create mock (raw, enriched) pair for deduplication."""
    raw_id = uuid.uuid4()
    raw = SimpleNamespace(
        id=raw_id,
        source=source,
        source_link=None,
        name_raw=name,
        inn=inn,
        ogrn=None,
        phones=["+79001234567"],
        emails=[],
        addresses=["Omsk"],
        contacts_json={},
        average_rating=average_rating,
        reviews_count=reviews_count,
    )
    enriched = SimpleNamespace(
        inn=inn,
        ogrn=None,
        name_normalized=name,
        entity_type="ЮЛ",
        phones_normalized=["+79001234567"],
        addresses_parsed=[{"raw": "Omsk"}],
        checks={},
        confidence_score=confidence,
        manual_review_required=False,
    )
    return raw, enriched


def test_dedup_reviews_count_is_none_after_merge():
    """After deduplication, reviews_count should be None (set later from factual data)."""
    dedup = Deduplicator(threshold=85)

    pair1 = _make_raw_enriched_pair(inn="5501234567", source="yandex", reviews_count=15)
    pair2 = _make_raw_enriched_pair(inn="5501234567", source="2gis", reviews_count=8)

    cards = dedup.run([pair1, pair2])

    assert len(cards) == 1
    card = cards[0]
    assert card.reviews_count is None  # Not summed from declared counters


def test_dedup_preserves_declared_total_in_contacts():
    """Declared total should be stored in contacts_json for analytics."""
    dedup = Deduplicator(threshold=85)

    pair1 = _make_raw_enriched_pair(inn="5501234567", source="yandex", reviews_count=15)
    pair2 = _make_raw_enriched_pair(inn="5501234567", source="2gis", reviews_count=8)

    cards = dedup.run([pair1, pair2])

    assert len(cards) == 1
    card = cards[0]
    assert card.contacts_json.get("declared_reviews_total") == 23


def test_dedup_single_record_reviews_count_none():
    """Even a single record should have reviews_count=None after dedup."""
    dedup = Deduplicator()
    pair = _make_raw_enriched_pair(reviews_count=42)
    cards = dedup.run([pair])
    assert len(cards) == 1
    assert cards[0].reviews_count is None


# ── gather_reviews: only factual review rows ─────────────────────────────


def _make_db_raw_with_reviews(raw_id: str, num_reviews: int):
    """Create a mock CompanyRaw ORM object with attached reviews."""
    reviews = []
    for i in range(num_reviews):
        reviews.append(
            SimpleNamespace(
                text=f"Review text {i}",
                rating=4.0,
                author=f"Author {i}",
                review_date=datetime(2026, 1, 1, tzinfo=UTC),
                source_link="https://example.com",
                source="yandex",
            )
        )
    return SimpleNamespace(id=raw_id, reviews=reviews)


def test_gather_reviews_returns_only_actual():
    """gather_reviews should return only actual review rows, not declared count."""
    card = CanonicalCard(
        name_normalized="Test",
        reviews_count=None,
        source_records=["raw-1", "raw-2"],
    )

    db_id_map = {
        "raw-1": _make_db_raw_with_reviews("raw-1", 3),
        "raw-2": _make_db_raw_with_reviews("raw-2", 2),
    }
    extra_reviews_map: dict = {}

    result = gather_reviews(card, db_id_map, extra_reviews_map)
    assert len(result) == 5


def test_gather_reviews_empty_when_no_reviews():
    """If no actual review rows exist, gather_reviews returns empty list."""
    card = CanonicalCard(
        name_normalized="Test",
        reviews_count=None,
        source_records=["raw-1"],
    )

    db_id_map = {
        "raw-1": _make_db_raw_with_reviews("raw-1", 0),
    }
    extra_reviews_map: dict = {}

    result = gather_reviews(card, db_id_map, extra_reviews_map)
    assert len(result) == 0


def test_gather_reviews_includes_extra_reviews():
    """Extra reviews from stage 2 (flamp/vk/otzovik) should be included."""
    card = CanonicalCard(
        name_normalized="Test",
        reviews_count=None,
        source_records=["raw-1"],
    )

    db_id_map = {
        "raw-1": _make_db_raw_with_reviews("raw-1", 2),
    }
    extra_reviews_map = {
        "raw-1": [
            RawReview(source="flamp", text="Flamp review", rating=3.0),
        ],
    }

    result = gather_reviews(card, db_id_map, extra_reviews_map)
    assert len(result) == 3
    sources = {r["source"] for r in result}
    assert "flamp" in sources
    assert "yandex" in sources


# ── Final reviews_count = factual count ──────────────────────────────────


def test_final_reviews_count_is_factual():
    """Simulate what enrichment_runner does: reviews_count = len(gathered)."""
    card = CanonicalCard(
        name_normalized="Test",
        reviews_count=None,  # Set by dedup
        source_records=["raw-1"],
    )

    db_id_map = {
        "raw-1": _make_db_raw_with_reviews("raw-1", 7),
    }
    extra_reviews_map: dict = {}

    reviews_for_ai = gather_reviews(card, db_id_map, extra_reviews_map)

    # This is what enrichment_runner._write_clean_rows does:
    card.reviews_count = len(reviews_for_ai) if reviews_for_ai else 0

    assert card.reviews_count == 7  # Factual, not inflated


def test_final_reviews_count_zero_when_no_texts():
    """If declared reviews_count was high but no actual texts, final count is 0."""
    card = CanonicalCard(
        name_normalized="Test",
        reviews_count=None,
        contacts_json={"declared_reviews_total": 50},  # Was declared high
        source_records=["raw-1"],
    )

    db_id_map = {
        "raw-1": _make_db_raw_with_reviews("raw-1", 0),  # No actual review texts
    }
    extra_reviews_map: dict = {}

    reviews_for_ai = gather_reviews(card, db_id_map, extra_reviews_map)
    card.reviews_count = len(reviews_for_ai) if reviews_for_ai else 0

    assert card.reviews_count == 0  # Factual zero, not inflated 50
