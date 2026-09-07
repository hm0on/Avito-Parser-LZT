"""LLM arbiter for ambiguous relevance decisions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx
import structlog
from openai import AsyncOpenAI

from src.collectors.base import RawCompany
from src.config import settings
from src.proxy import iter_proxy_urls, openai_proxy_manager

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "Ты классификатор релевантности карточек бизнеса. "
    "Цель: оставить только сантехнических подрядчиков в Омске и Омской области. "
    "Отвечай только JSON без markdown."
)


@dataclass(slots=True)
class LlmRelevanceDecision:
    domain_pass: bool
    geo_pass: bool
    confidence: float
    reason_codes: list[str] = field(default_factory=list)


class LlmRelevanceArbiter:
    async def classify(self, *, raw: RawCompany, keyword: str, snippet: str) -> LlmRelevanceDecision | None:
        if not settings.relevance_use_openai or not settings.openai_api_key:
            return None

        prompt = self._build_prompt(raw=raw, keyword=keyword, snippet=snippet)
        attempts = max(1, settings.relevance_max_attempts)

        for attempt, proxy_url in enumerate(self._iter_proxy_candidates(attempts=attempts), start=1):
            try:
                async with httpx.AsyncClient(
                    proxy=proxy_url,
                    timeout=float(settings.relevance_timeout_seconds),
                ) as http_client:
                    client = AsyncOpenAI(api_key=settings.openai_api_key, http_client=http_client)
                    response = await client.chat.completions.create(
                        model=settings.relevance_model,
                        temperature=0.0,
                        max_tokens=350,
                        messages=[
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                    )

                content = (response.choices[0].message.content or "").strip()
                parsed = _extract_json(content)
                if parsed is None:
                    raise ValueError("invalid JSON from relevance model")
                decision = _normalize(parsed)
                if decision is None:
                    raise ValueError("invalid relevance payload")
                return decision
            except Exception as exc:
                log.warning(
                    "relevance.llm.error",
                    attempt=attempt,
                    proxy=proxy_url or "direct",
                    error=str(exc)[:240],
                )
        return None

    def _iter_proxy_candidates(self, *, attempts: int):
        if openai_proxy_manager.count > 0:
            yield from iter_proxy_urls(
                openai_proxy_manager,
                purpose="relevance llm",
                max_attempts=attempts,
            )
            return
        for _ in range(attempts):
            yield None

    def _build_prompt(self, *, raw: RawCompany, keyword: str, snippet: str) -> str:
        addresses = ", ".join(raw.addresses or [])
        return (
            "Верни JSON формата:\n"
            "{"
            '"domain_pass": true|false, '
            '"geo_pass": true|false, '
            '"confidence": number(0..1), '
            '"reason_codes": ["..."]'
            "}\n\n"
            f"source={raw.source}\n"
            f"keyword={keyword}\n"
            f"name={raw.name_raw or ''}\n"
            f"address={addresses}\n"
            f"source_link={raw.source_link or ''}\n"
            f"snippet={snippet[:1500]}\n"
        )


def _extract_json(text: str) -> dict | None:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _normalize(payload: dict) -> LlmRelevanceDecision | None:
    confidence_raw = payload.get("confidence")
    try:
        confidence = float(confidence_raw)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reason_codes = payload.get("reason_codes")
    if not isinstance(reason_codes, list):
        reason_codes = []
    reason_codes = [str(item)[:80] for item in reason_codes if str(item).strip()][:5]

    return LlmRelevanceDecision(
        domain_pass=bool(payload.get("domain_pass")),
        geo_pass=bool(payload.get("geo_pass")),
        confidence=confidence,
        reason_codes=reason_codes,
    )
