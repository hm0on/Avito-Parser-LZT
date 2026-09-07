"""OpenAI fallback checker for company profile hints when registries fail."""

from __future__ import annotations

import json
import re

import httpx
import structlog
from openai import AsyncOpenAI

from src.config import settings
from src.enrichment.registries.base import AbstractChecker, CheckResult
from src.proxy import iter_proxy_urls, openai_proxy_manager

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "Ты помощник по проверке юрлиц. Верни только JSON без markdown. "
    "Нельзя выдумывать факты. Если не уверен, ставь null и found=false. "
    "Допустимые entity_type: 'ЮЛ', 'ИП', 'unknown'. "
    "Допустимые status: 'active', 'liquidated', 'bankrupt', 'unknown'."
)

_USER_TEMPLATE = """
Найди сведения о компании и верни JSON:
{{
  "found": true|false,
  "name_normalized": "строка или null",
  "entity_type": "ЮЛ|ИП|unknown",
  "inn": "10/12 цифр или null",
  "ogrn": "13/15 цифр или null",
  "status": "active|liquidated|bankrupt|unknown",
  "summary": "кратко 1-2 предложения",
  "confidence": число от 0 до 1
}}

Вход:
- company_name: "{name}"
- inn_hint: "{inn}"
- ogrn_hint: "{ogrn}"
"""


class OpenAICompanyFallbackChecker(AbstractChecker):
    registry_name = "openai_company_fallback"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
    ) -> CheckResult:
        if not settings.openai_company_fallback_enabled:
            return CheckResult(registry=self.registry_name, found=False, error="openai fallback disabled")
        if not settings.openai_api_key:
            return CheckResult(registry=self.registry_name, found=False, error="OPENAI_API_KEY not set")
        if not name:
            return CheckResult(registry=self.registry_name, found=False, error="company name required")

        prompt = _USER_TEMPLATE.format(name=name, inn=inn or "", ogrn=ogrn or "")

        attempts_done = 0
        for proxy_url in self._iter_proxy_candidates():
            attempts_done += 1
            try:
                async with httpx.AsyncClient(proxy=proxy_url, timeout=float(settings.openai_company_fallback_timeout_seconds)) as http_client:
                    client = AsyncOpenAI(api_key=settings.openai_api_key, http_client=http_client)
                    resp = await client.chat.completions.create(
                        model=settings.openai_company_fallback_model,
                        temperature=0.0,
                        max_tokens=350,
                        messages=[
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": prompt},
                        ],
                    )
                content = (resp.choices[0].message.content or "").strip()
                payload = _parse_json_payload(content)
                if payload is None:
                    raise ValueError("invalid JSON response")

                details = _normalize_payload(payload)
                found = bool(details.get("found"))
                status = details.get("status")
                log.info(
                    "openai_company_fallback.done",
                    company=name,
                    found=found,
                    confidence=details.get("confidence"),
                    proxy=proxy_url or "direct",
                )
                return CheckResult(
                    registry=self.registry_name,
                    found=found,
                    status=status if isinstance(status, str) else None,
                    details=details,
                )
            except Exception as exc:
                log.warning(
                    "openai_company_fallback.error",
                    company=name,
                    attempt=attempts_done,
                    proxy=proxy_url or "direct",
                    error=str(exc)[:220],
                )

        return CheckResult(
            registry=self.registry_name,
            found=False,
            error=f"all attempts failed ({attempts_done})",
        )

    def _iter_proxy_candidates(self):
        max_attempts = max(1, settings.openai_company_fallback_max_attempts)
        if settings.openai_company_fallback_use_proxy and openai_proxy_manager.count > 0:
            for proxy_url in iter_proxy_urls(
                openai_proxy_manager,
                purpose="openai company fallback",
                max_attempts=max_attempts,
            ):
                yield proxy_url
            return

        for _ in range(max_attempts):
            yield None


def map_openai_fallback_to_registries(openai_result: CheckResult) -> dict[str, dict]:
    if not openai_result.found:
        return {}
    details = openai_result.details or {}
    confidence = float(details.get("confidence") or 0.0)
    inn = details.get("inn")
    ogrn = details.get("ogrn")
    if confidence < 0.85 or (not inn and not ogrn):
        return {}

    status_raw = str(details.get("status") or "").lower()
    mapped_status = "ACTIVE"
    if status_raw in {"liquidated", "bankrupt"}:
        mapped_status = status_raw
    elif status_raw not in {"active", "unknown"}:
        mapped_status = "ACTIVE"

    return {
        "dadata_fns": {
            "registry": "dadata_fns",
            "found": True,
            "status": mapped_status,
            "details": {
                "name": details.get("name_normalized"),
                "inn": inn,
                "ogrn": ogrn,
                "entity_type": details.get("entity_type") or "unknown",
                "source": "openai_company_fallback",
                "unverified": True,
            },
            "error": None,
        }
    }


def _parse_json_payload(text: str) -> dict | None:
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


def _normalize_payload(payload: dict) -> dict:
    def _norm_digits(value: object, lengths: tuple[int, ...]) -> str | None:
        if not isinstance(value, str):
            return None
        digits = re.sub(r"\D", "", value)
        return digits if len(digits) in lengths else None

    confidence_raw = payload.get("confidence")
    try:
        confidence = float(confidence_raw)
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    status = str(payload.get("status") or "unknown").lower()
    if status not in {"active", "liquidated", "bankrupt", "unknown"}:
        status = "unknown"

    entity_type = str(payload.get("entity_type") or "unknown")
    if entity_type not in {"ЮЛ", "ИП", "unknown"}:
        entity_type = "unknown"

    found_raw = payload.get("found")
    found = bool(found_raw) and confidence >= 0.35
    summary = payload.get("summary") or ""
    if not isinstance(summary, str) or not summary.strip():
        summary = "Надежные сведения не подтверждены автоматическим fallback-поиском."

    return {
        "found": found,
        "name_normalized": (payload.get("name_normalized") or None),
        "entity_type": entity_type,
        "inn": _norm_digits(payload.get("inn"), (10, 12)),
        "ogrn": _norm_digits(payload.get("ogrn"), (13, 15)),
        "status": status,
        "summary": summary.strip(),
        "confidence": confidence,
        "unverified": True,
    }
