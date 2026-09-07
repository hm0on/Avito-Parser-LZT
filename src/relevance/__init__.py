"""Precision-first relevance service."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.collectors.base import RawCompany
from src.config import settings
from src.relevance.domain_filter import DomainDecision, evaluate_domain
from src.relevance.geo_filter import GeoDecision, evaluate_geo
from src.relevance.llm_relevance import LlmRelevanceArbiter


@dataclass(slots=True)
class RelevanceDecision:
    domain_score: float
    domain_pass: bool
    geo_pass: bool
    confidence: float
    reason_codes: list[str] = field(default_factory=list)
    domain_allow_hits: list[str] = field(default_factory=list)
    domain_deny_hits: list[str] = field(default_factory=list)
    geo_evidence: list[str] = field(default_factory=list)
    classifier: str = "rules"

    def to_dict(self) -> dict:
        return {
            "domain_score": self.domain_score,
            "domain_pass": self.domain_pass,
            "geo_pass": self.geo_pass,
            "confidence": self.confidence,
            "reason_codes": self.reason_codes,
            "domain_allow_hits": self.domain_allow_hits,
            "domain_deny_hits": self.domain_deny_hits,
            "geo_evidence": self.geo_evidence,
            "classifier": self.classifier,
        }


class RelevanceService:
    def __init__(self) -> None:
        self._llm = LlmRelevanceArbiter()

    async def decide(self, *, raw: RawCompany, keyword: str) -> RelevanceDecision:
        snippet = _build_snippet(raw)
        domain: DomainDecision = evaluate_domain(name=raw.name_raw, keyword=keyword, snippet=snippet)
        geo: GeoDecision = evaluate_geo(raw)

        base_conf = max(
            0.0,
            min(
                1.0,
                (domain.score * 0.7) + ((1.0 if geo.geo_pass else 0.0) * 0.3),
            ),
        )

        reason_codes = list(dict.fromkeys([*domain.reason_codes, *geo.reason_codes]))
        decision = RelevanceDecision(
            domain_score=round(domain.score, 4),
            domain_pass=domain.domain_pass,
            geo_pass=geo.geo_pass,
            confidence=round(base_conf, 4),
            reason_codes=reason_codes[:8],
            domain_allow_hits=domain.allow_hits[:8],
            domain_deny_hits=domain.deny_hits[:8],
            geo_evidence=geo.evidence[:8],
            classifier="rules",
        )

        needs_llm = (
            domain.ambiguous
            or decision.confidence < settings.relevance_min_confidence
            or (decision.domain_pass and not decision.geo_pass)
        )
        if not needs_llm:
            return decision

        llm = await self._llm.classify(raw=raw, keyword=keyword, snippet=snippet)
        if llm is None:
            return decision

        merged_reasons = list(dict.fromkeys([*decision.reason_codes, *llm.reason_codes, "llm_relevance"]))
        return RelevanceDecision(
            domain_score=decision.domain_score,
            domain_pass=llm.domain_pass,
            geo_pass=llm.geo_pass,
            confidence=round(max(decision.confidence, llm.confidence), 4),
            reason_codes=merged_reasons[:8],
            domain_allow_hits=decision.domain_allow_hits,
            domain_deny_hits=decision.domain_deny_hits,
            geo_evidence=decision.geo_evidence,
            classifier="llm",
        )


def _build_snippet(raw: RawCompany) -> str:
    payload = ""
    if raw.raw_payload:
        try:
            payload = json.dumps(raw.raw_payload, ensure_ascii=False)
        except Exception:
            payload = str(raw.raw_payload)

    return " ".join(
        part
        for part in (
            raw.name_raw or "",
            " ".join(raw.addresses or []),
            payload[:2000],
        )
        if part
    )
