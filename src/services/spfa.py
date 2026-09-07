"""SPFA client for resolving Avito temporary phone numbers by ad id."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from src.config import settings


class SpfaConfigError(RuntimeError):
    """Raised when SPFA credentials are missing."""


class SpfaLookupError(RuntimeError):
    """Raised when SPFA returns an unusable response."""


@dataclass(slots=True)
class SpfaLookupResult:
    ad_id: str
    phone: str
    provider_status: int
    raw_response: dict[str, Any]


class SpfaClient:
    async def lookup_phone_by_ad_id(self, ad_id: str) -> SpfaLookupResult:
        api_key = settings.spfa_api_key.strip()
        if not api_key:
            raise SpfaConfigError("SPFA_API_KEY must be configured")

        clean_ad_id = str(ad_id).strip()
        if not clean_ad_id:
            raise SpfaLookupError("Avito ad id is empty")

        payload = {
            "api_key": api_key,
            "ads": [clean_ad_id],
        }

        async with httpx.AsyncClient(
            timeout=max(5.0, float(settings.spfa_timeout_seconds)),
            follow_redirects=True,
        ) as client:
            response = await client.post(
                settings.spfa_phone_endpoint,
                json=payload,
                headers={"Accept": "application/json"},
            )

        response_payload = _parse_response_payload(response)
        phone = _extract_phone(response_payload)
        if not phone:
            error_text = _extract_error_text(response_payload) or f"SPFA returned no phone for status {response.status_code}"
            raise SpfaLookupError(error_text)

        return SpfaLookupResult(
            ad_id=clean_ad_id,
            phone=phone,
            provider_status=response.status_code,
            raw_response=response_payload,
        )


def extract_avito_ad_id(avito_url: str | None) -> str | None:
    if not avito_url:
        return None

    parsed = urlparse(avito_url.strip())
    if parsed.query:
        iid_values = parse_qs(parsed.query).get("iid")
        if iid_values:
            ad_id = _normalize_ad_id(iid_values[0])
            if ad_id:
                return ad_id

    path = parsed.path.rstrip("/")
    if "_" not in path:
        return None

    ad_id = path.rsplit("_", 1)[-1]
    return _normalize_ad_id(ad_id)


def _normalize_ad_id(raw: Any) -> str | None:
    value = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return value or None


def _parse_response_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SpfaLookupError("SPFA returned non-JSON response") from exc

    if isinstance(payload, dict):
        return payload
    return {"result": payload}


def _extract_phone(payload: dict[str, Any]) -> str | None:
    direct_candidates = (
        payload.get("phone"),
        payload.get("number"),
        payload.get("tel"),
        payload.get("mobile"),
    )
    for candidate in direct_candidates:
        phone = _normalize_phone(candidate)
        if phone:
            return phone

    for bucket_key in ("data", "result", "results", "response"):
        bucket = payload.get(bucket_key)
        if isinstance(bucket, dict):
            nested_candidates = (
                bucket.get("phone"),
                bucket.get("number"),
                bucket.get("tel"),
                bucket.get("mobile"),
            )
            for candidate in nested_candidates:
                phone = _normalize_phone(candidate)
                if phone:
                    return phone

            for value in bucket.values():
                phone = _extract_phone_from_item(value)
                if phone:
                    return phone
        elif isinstance(bucket, list):
            for item in bucket:
                phone = _extract_phone_from_item(item)
                if phone:
                    return phone

    return None


def _extract_phone_from_item(item: Any) -> str | None:
    if isinstance(item, dict):
        for key in ("phone", "number", "tel", "mobile"):
            phone = _normalize_phone(item.get(key))
            if phone:
                return phone
        for value in item.values():
            phone = _extract_phone_from_item(value)
            if phone:
                return phone
    elif isinstance(item, list):
        for value in item:
            phone = _extract_phone_from_item(value)
            if phone:
                return phone
    return None


def _extract_error_text(payload: dict[str, Any]) -> str | None:
    for key in ("error", "message", "detail", "msg"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for bucket_key in ("data", "result", "results", "response"):
        bucket = payload.get(bucket_key)
        if not isinstance(bucket, dict):
            continue
        for key in ("error", "message", "detail", "msg"):
            value = bucket.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _normalize_phone(value: Any) -> str | None:
    if value is None:
        return None

    phone = str(value).strip()
    return phone or None
