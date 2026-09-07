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
    identity_key: str | None = None
    merge_group_id: str | None = None
    inn: str | None = None
    ogrn: str | None = None
    entity_type: str | None = "unknown"
    source_name_primary: str | None = None
    legal_name: str | None = None
    legal_verified: bool = False
    relevance_score: float | None = None
    geo_verified: bool = False
    quality_flags: dict | None = None

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
    source_links: list[dict] = field(default_factory=list)  # [{source, url}]
    similar_company_ids: list[str] = field(default_factory=list)


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

        groups = self.detect_groups(enriched_records)

        canonical: list[CanonicalCard] = [self._merge_group(group) for group in groups]

        log.info("dedup.done", input=len(enriched_records), output=len(canonical))
        return canonical

    def detect_groups(
        self,
        enriched_records: list[tuple[CompanyRaw, CompanyEnriched]],
    ) -> list[list[tuple[CompanyRaw, CompanyEnriched]]]:
        """Detect duplicate groups without deciding how they will be persisted."""
        # Step 1: group by INN
        inn_groups: dict[str, list[tuple[CompanyRaw, CompanyEnriched]]] = {}
        no_inn: list[tuple[CompanyRaw, CompanyEnriched]] = []

        for raw, enriched in enriched_records:
            inn = enriched.inn or raw.inn
            if inn:
                inn_groups.setdefault(inn, []).append((raw, enriched))
            else:
                no_inn.append((raw, enriched))

        groups: list[list[tuple[CompanyRaw, CompanyEnriched]]] = []

        # Merge INN groups
        groups.extend(inn_groups.values())

        # Step 2: fuzzy match remaining no-INN records
        fuzzy_groups = self._fuzzy_group(no_inn)
        groups.extend(fuzzy_groups)
        return groups

    def build_single_card(
        self,
        raw: CompanyRaw,
        enriched: CompanyEnriched,
        *,
        identity_key: str,
    ) -> CanonicalCard:
        """Build a one-to-one clean card without auto-merging similar companies."""
        phones = list(dict.fromkeys((enriched.phones_normalized or raw.phones or [])))
        emails = list(dict.fromkeys(raw.emails or []))
        addresses = list(enriched.addresses_parsed or raw.addresses or [])
        contacts = dict(raw.contacts_json or {})
        contacts["declared_reviews_total"] = raw.reviews_count or 0

        primary_checks = enriched.checks if isinstance(enriched.checks, dict) else {}
        legal_match = primary_checks.get("legal_match", {}) if isinstance(primary_checks, dict) else {}
        legal_evidence = legal_match.get("evidence", {}) if isinstance(legal_match, dict) else {}
        relevance_details = (
            enriched.relevance_details
            if isinstance(getattr(enriched, "relevance_details", None), dict)
            else {}
        )

        source_links: list[dict] = []
        if raw.source_link:
            source_links.append({"source": raw.source, "url": raw.source_link})

        return CanonicalCard(
            identity_key=identity_key,
            name_normalized=enriched.name_normalized or raw.name_raw or "",
            inn=enriched.inn,
            ogrn=enriched.ogrn,
            entity_type=enriched.entity_type,
            source_name_primary=raw.name_raw,
            legal_name=legal_evidence.get("legal_name"),
            legal_verified=bool(getattr(enriched, "legal_verified", False)),
            relevance_score=getattr(enriched, "relevance_score", None),
            geo_verified=bool(relevance_details.get("geo_pass")),
            quality_flags={
                "legal_match_method": getattr(enriched, "legal_match_method", None),
                "legal_match_score": getattr(enriched, "legal_match_score", None),
                "legal_override_reason": legal_match.get("override_reason"),
                "relevance_classifier": relevance_details.get("classifier"),
                "relevance_reason_codes": relevance_details.get("reason_codes"),
            },
            phones=phones,
            emails=emails,
            addresses=addresses,
            contacts_json=contacts,
            average_rating=raw.average_rating,
            reviews_count=None,
            checks=primary_checks,
            manual_review_required=bool(getattr(enriched, "manual_review_required", False)),
            source_records=[str(raw.id)],
            merged_sources=[raw.source],
            source_links=source_links,
        )

    def merge_group(
        self,
        group: list[tuple[CompanyRaw, CompanyEnriched]],
        *,
        primary_identity_key: str | None = None,
        identity_key_getter=None,
    ) -> CanonicalCard:
        """Merge a specific group, optionally forcing the primary source record."""
        if primary_identity_key and identity_key_getter is not None:
            for raw, enriched in group:
                if identity_key_getter(raw, enriched) == primary_identity_key:
                    reordered = [(raw, enriched)] + [pair for pair in group if pair[0] is not raw]
                    return self._merge_group(reordered, force_primary_pair=(raw, enriched))
        return self._merge_group(group)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    def _merge_group(
        self,
        group: list[tuple[CompanyRaw, CompanyEnriched]],
        *,
        force_primary_pair: tuple[CompanyRaw, CompanyEnriched] | None = None,
    ) -> CanonicalCard:
        """Merge a group of (raw, enriched) pairs into one CanonicalCard."""
        # Pick the richest record (highest confidence_score)
        if force_primary_pair is not None:
            primary_raw, primary_enriched = force_primary_pair
        else:
            primary_raw, primary_enriched = max(
                group,
                key=lambda pair: pair[1].confidence_score or 0,
            )

        phones: set[str] = set()
        emails: set[str] = set()
        addresses: list = []
        sources: set[str] = set()
        source_links: list[dict] = []
        seen_urls: set[str] = set()
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
            if raw.source_link and raw.source_link not in seen_urls:
                source_links.append({"source": raw.source, "url": raw.source_link})
                seen_urls.add(raw.source_link)
            if enriched.manual_review_required:
                manual_review = True

        # Aggregate ratings (weighted by reviews_count) — only for initial
        # estimate. Final reviews_count is set by enrichment_runner from
        # actual gathered review rows, not declared listing counters.
        total_weight = 0
        weighted_sum = 0.0
        declared_total = 0
        for raw, _ in group:
            if raw.average_rating and raw.reviews_count:
                weighted_sum += raw.average_rating * raw.reviews_count
                total_weight += raw.reviews_count
            declared_total += raw.reviews_count or 0

        average_rating = weighted_sum / total_weight if total_weight else None

        # Keep declared counter in contacts_json for analytics, but set
        # reviews_count to None so enrichment_runner fills it from factual data.
        contacts = dict(primary_raw.contacts_json or {})
        contacts["declared_reviews_total"] = declared_total

        primary_checks = primary_enriched.checks if isinstance(primary_enriched.checks, dict) else {}
        legal_match = primary_checks.get("legal_match", {}) if isinstance(primary_checks, dict) else {}
        legal_evidence = legal_match.get("evidence", {}) if isinstance(legal_match, dict) else {}
        relevance_details = (
            primary_enriched.relevance_details
            if isinstance(getattr(primary_enriched, "relevance_details", None), dict)
            else {}
        )

        return CanonicalCard(
            name_normalized=primary_enriched.name_normalized or primary_raw.name_raw or "",
            inn=primary_enriched.inn,
            ogrn=primary_enriched.ogrn,
            entity_type=primary_enriched.entity_type,
            source_name_primary=primary_raw.name_raw,
            legal_name=legal_evidence.get("legal_name"),
            legal_verified=bool(getattr(primary_enriched, "legal_verified", False)),
            relevance_score=getattr(primary_enriched, "relevance_score", None),
            geo_verified=bool(relevance_details.get("geo_pass")),
            quality_flags={
                "legal_match_method": getattr(primary_enriched, "legal_match_method", None),
                "legal_match_score": getattr(primary_enriched, "legal_match_score", None),
                "legal_override_reason": legal_match.get("override_reason"),
                "relevance_classifier": relevance_details.get("classifier"),
                "relevance_reason_codes": relevance_details.get("reason_codes"),
            },
            phones=list(phones),
            emails=list(emails),
            addresses=addresses,
            contacts_json=contacts,
            average_rating=round(average_rating, 2) if average_rating else None,
            reviews_count=None,  # Will be set from factual gathered reviews
            checks=primary_checks,
            manual_review_required=manual_review,
            source_records=[str(raw.id) for raw, _ in group],
            merged_sources=sorted(sources),
            source_links=source_links,
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
