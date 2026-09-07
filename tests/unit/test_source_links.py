"""Tests for source_links population through dedup → canonical_to_orm → CompanyClean."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest

from src.deduplication.deduplicator import CanonicalCard, Deduplicator
from src.pipeline.common import canonical_to_orm


def _make_raw(source: str, source_link: str | None, inn: str | None = None, name: str = "Test"):
    raw = MagicMock()
    raw.id = uuid.uuid4()
    raw.source = source
    raw.source_link = source_link
    raw.name_raw = name
    raw.phones = []
    raw.emails = []
    raw.addresses = []
    raw.contacts_json = {}
    raw.inn = inn
    raw.ogrn = None
    raw.average_rating = 4.0
    raw.reviews_count = 5
    return raw


def _make_enriched(inn: str | None = None, confidence: int = 50):
    enriched = MagicMock()
    enriched.inn = inn
    enriched.ogrn = None
    enriched.name_normalized = "Test Company"
    enriched.entity_type = "ЮЛ"
    enriched.phones_normalized = []
    enriched.addresses_parsed = []
    enriched.confidence_score = confidence
    enriched.checks = {}
    enriched.manual_review_required = False
    return enriched


class TestSourceLinksDedup:
    def test_single_record_has_source_link(self):
        raw = _make_raw("avito", "https://avito.ru/company/123")
        enriched = _make_enriched()
        dedup = Deduplicator()
        cards = dedup.run([(raw, enriched)])
        assert len(cards) == 1
        assert cards[0].source_links == [{"source": "avito", "url": "https://avito.ru/company/123"}]

    def test_merged_group_collects_all_links(self):
        raw1 = _make_raw("avito", "https://avito.ru/company/1", inn="1234567890")
        raw2 = _make_raw("yandex", "https://yandex.ru/maps/org/999", inn="1234567890")
        enriched1 = _make_enriched(inn="1234567890", confidence=60)
        enriched2 = _make_enriched(inn="1234567890", confidence=50)
        dedup = Deduplicator()
        cards = dedup.run([(raw1, enriched1), (raw2, enriched2)])
        assert len(cards) == 1
        links = cards[0].source_links
        assert len(links) == 2
        sources = {l["source"] for l in links}
        assert sources == {"avito", "yandex"}

    def test_no_source_link_gives_empty(self):
        raw = _make_raw("avito", None)
        enriched = _make_enriched()
        dedup = Deduplicator()
        cards = dedup.run([(raw, enriched)])
        assert cards[0].source_links == []

    def test_duplicate_urls_deduplicated(self):
        raw1 = _make_raw("avito", "https://avito.ru/company/1", inn="111")
        raw2 = _make_raw("avito", "https://avito.ru/company/1", inn="111")
        enriched1 = _make_enriched(inn="111", confidence=60)
        enriched2 = _make_enriched(inn="111", confidence=50)
        dedup = Deduplicator()
        cards = dedup.run([(raw1, enriched1), (raw2, enriched2)])
        assert len(cards[0].source_links) == 1


class TestSourceLinksORM:
    def test_canonical_to_orm_passes_source_links(self):
        card = CanonicalCard(
            name_normalized="Test",
            source_links=[{"source": "avito", "url": "https://avito.ru/1"}],
        )
        orm = canonical_to_orm(card, "summary", "green", [], week_start=date(2026, 3, 1))
        assert orm.source_links == [{"source": "avito", "url": "https://avito.ru/1"}]

    def test_canonical_to_orm_empty_source_links(self):
        card = CanonicalCard(name_normalized="Test")
        orm = canonical_to_orm(card, None, "green", [], week_start=date(2026, 3, 1))
        assert orm.source_links == []

    def test_three_sources_merged(self):
        raw_avito = _make_raw("avito", "https://avito.ru/1", inn="555")
        raw_yandex = _make_raw("yandex", "https://yandex.ru/org/1", inn="555")
        raw_2gis = _make_raw("2gis", "https://2gis.ru/firm/1", inn="555")
        e1 = _make_enriched(inn="555", confidence=70)
        e2 = _make_enriched(inn="555", confidence=60)
        e3 = _make_enriched(inn="555", confidence=50)
        dedup = Deduplicator()
        cards = dedup.run([(raw_avito, e1), (raw_yandex, e2), (raw_2gis, e3)])
        assert len(cards) == 1
        assert len(cards[0].source_links) == 3
        urls = {l["url"] for l in cards[0].source_links}
        assert "https://avito.ru/1" in urls
        assert "https://yandex.ru/org/1" in urls
        assert "https://2gis.ru/firm/1" in urls
