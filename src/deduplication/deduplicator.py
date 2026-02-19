"""Deduplication engine: INN-merge + rapidfuzz composite scoring."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import structlog
from rapidfuzz import fuzz

from src.config import settings
from src.database.models import CompanyEnriched, CompanyRaw

log = structlog.get_logger(__name__)


@dataclass
class CanonicalCard:
    """Merged canonical company record ready to be written to companies_omsk_clean."""

    name_normalized: str
    inn: str | None = None
    ogrn: str | None = None
    entity_type: str | None = "unknown"

    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    addresses: list[Any] = field(default_factory=list)
    contacts_json: dict = field(default_factory=dict)

    average_rating: float | None = None
    reviews_count: int | None = None
    reviews_sample: list[dict] = field(default_factory=list)

    checks: dict = field(default_factory=dict)
    manual_review_required: bool = False

    # Merge provenance
    source_records: list[str] = field(default_factory=list)  # raw UUIDs
    merged_sources: list[str] = field(default_factory=list)  # avito/2gis/...


class Deduplicator:
    """Groups and merges CompanyEnriched records into CanonicalCards."""

    def __init__(self, threshold: int | None = None) -> None:
        self.threshold = threshold or settings.dedup_threshold

    def run(
        self,
        enriched_records: list[tuple[CompanyRaw, CompanyEnriched]],
    ) -> list[CanonicalCard]:
        """Full deduplication pipeline.

        Args:
            enriched_records: list of (raw, enriched) pairs.

        Returns:
            List of deduplicated CanonicalCard objects.
        """
        log.info("dedup.start", records=len(enriched_records))

        # Step 1: group by INN
        inn_groups: dict[str, list[tuple[CompanyRaw, CompanyEnriched]]] = {}
        no_inn: list[tuple[CompanyRaw, CompanyEnriched]] = []

        for raw, enriched in enriched_records:
            inn = enriched.inn or raw.inn
            if inn:
                inn_groups.setdefault(inn, []).append((raw, enriched))
            else:
                no_inn.append((raw, enriched))

        canonical: list[CanonicalCard] = []

        # Merge INN groups
        for inn, group in inn_groups.items():
            card = self._merge_group(group)
            canonical.append(card)

        # Step 2: fuzzy match remaining no-INN records
        fuzzy_groups = self._fuzzy_group(no_inn)
        for group in fuzzy_groups:
            card = self._merge_group(group)
            canonical.append(card)

        log.info("dedup.done", input=len(enriched_records), output=len(canonical))
        return canonical

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    def _merge_group(
        self,
        group: list[tuple[CompanyRaw, CompanyEnriched]],
    ) -> CanonicalCard:
        """Merge a group of (raw, enriched) pairs into one CanonicalCard."""
        # Pick the richest record (highest confidence_score)
        primary_raw, primary_enriched = max(
            group,
            key=lambda pair: pair[1].confidence_score or 0,
        )

        phones: set[str] = set()
        emails: set[str] = set()
        addresses: list = []
        sources: set[str] = set()
        manual_review = False

        for raw, enriched in group:
            for p in (enriched.phones_normalized or raw.phones or []):
                phones.add(p)
            for e in (raw.emails or []):
                emails.add(e)
            for a in (enriched.addresses_parsed or raw.addresses or []):
                if a and a not in addresses:
                    addresses.append(a)
            sources.add(raw.source)
            if enriched.manual_review_required:
                manual_review = True

        # Aggregate ratings (weighted by reviews_count)
        total_weight = 0
        weighted_sum = 0.0
        total_reviews = 0
        for raw, _ in group:
            if raw.average_rating and raw.reviews_count:
                weighted_sum += raw.average_rating * raw.reviews_count
                total_weight += raw.reviews_count
            total_reviews += raw.reviews_count or 0

        average_rating = weighted_sum / total_weight if total_weight else None

        return CanonicalCard(
            name_normalized=primary_enriched.name_normalized or primary_raw.name_raw or "",
            inn=primary_enriched.inn,
            ogrn=primary_enriched.ogrn,
            entity_type=primary_enriched.entity_type,
            phones=list(phones),
            emails=list(emails),
            addresses=addresses,
            contacts_json=primary_raw.contacts_json or {},
            average_rating=round(average_rating, 2) if average_rating else None,
            reviews_count=total_reviews or None,
            checks=primary_enriched.checks or {},
            manual_review_required=manual_review,
            source_records=[str(raw.id) for raw, _ in group],
            merged_sources=sorted(sources),
        )

    def _fuzzy_group(
        self,
        records: list[tuple[CompanyRaw, CompanyEnriched]],
    ) -> list[list[tuple[CompanyRaw, CompanyEnriched]]]:
        """Cluster no-INN records using composite fuzzy score."""
        n = len(records)
        parent = list(range(n))  # Union-Find

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            parent[find(i)] = find(j)

        for i, j in combinations(range(n), 2):
            raw_i, enriched_i = records[i]
            raw_j, enriched_j = records[j]
            score = self._composite_score(raw_i, enriched_i, raw_j, enriched_j)

            if score >= self.threshold:
                union(i, j)
            elif 70 <= score < self.threshold:
                log.debug(
                    "dedup.probable_duplicate",
                    name_a=raw_i.name_raw,
                    name_b=raw_j.name_raw,
                    score=score,
                )

        # Collect groups
        groups: dict[int, list] = {}
        for i in range(n):
            root = find(i)
            groups.setdefault(root, []).append(records[i])

        return list(groups.values())

    def _composite_score(
        self,
        raw_a: CompanyRaw,
        enriched_a: CompanyEnriched,
        raw_b: CompanyRaw,
        enriched_b: CompanyEnriched,
    ) -> float:
        """Compute composite similarity score (0–100)."""
        name_a = enriched_a.name_normalized or raw_a.name_raw or ""
        name_b = enriched_b.name_normalized or raw_b.name_raw or ""
        name_sim = fuzz.token_sort_ratio(name_a, name_b)

        phones_a: set[str] = set(enriched_a.phones_normalized or raw_a.phones or [])
        phones_b: set[str] = set(enriched_b.phones_normalized or raw_b.phones or [])
        phone_match = 100.0 if phones_a & phones_b else 0.0

        # Region comparison (very coarse — same city = match)
        region_match = 100.0  # Both records are from Omsk by construction

        composite = 0.6 * name_sim + 0.3 * phone_match + 0.1 * region_match
        return composite
