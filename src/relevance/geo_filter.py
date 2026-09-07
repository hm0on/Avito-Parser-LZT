"""Geo relevance checks for Omsk and Omsk Oblast."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.collectors.base import RawCompany
from src.config import settings


@dataclass(slots=True)
class GeoDecision:
    geo_pass: bool
    score: float
    evidence: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)


def evaluate_geo(raw: RawCompany) -> GeoDecision:
    terms = settings.target_geo_terms_list
    if not terms:
        return GeoDecision(geo_pass=True, score=1.0, reason_codes=["geo_terms_empty"])

    payload_text = ""
    if raw.raw_payload:
        try:
            payload_text = json.dumps(raw.raw_payload, ensure_ascii=False)
        except Exception:
            payload_text = str(raw.raw_payload)

    haystack = " ".join(
        part.lower()
        for part in (
            raw.name_raw or "",
            " ".join(raw.addresses or []),
            raw.source_link or "",
            payload_text,
        )
        if part
    )

    matched = [term for term in terms if term in haystack]
    if matched:
        return GeoDecision(
            geo_pass=True,
            score=min(1.0, 0.7 + 0.05 * len(matched)),
            evidence=matched[:8],
            reason_codes=["geo_term_match"],
        )

    return GeoDecision(
        geo_pass=False,
        score=0.0,
        evidence=[],
        reason_codes=["geo_out_of_scope"],
    )
