"""Unit tests for OpenAI company fallback helpers."""

from src.enrichment.registries.base import CheckResult
from src.enrichment.registries.openai_company_fallback import (
    _normalize_payload,
    _parse_json_payload,
    map_openai_fallback_to_registries,
)


def test_parse_json_payload_plain():
    payload = _parse_json_payload('{"found": true, "inn": "5501234567"}')
    assert payload is not None
    assert payload["found"] is True


def test_parse_json_payload_with_text_wrapper():
    payload = _parse_json_payload('Result: {"found": false, "status": "unknown"} end')
    assert payload is not None
    assert payload["found"] is False


def test_normalize_payload():
    normalized = _normalize_payload(
        {
            "found": True,
            "name_normalized": "ООО Тест",
            "entity_type": "ЮЛ",
            "inn": "ИНН 5501234567",
            "ogrn": "ОГРН 1155500001234",
            "status": "active",
            "summary": "ok",
            "confidence": 0.92,
        }
    )
    assert normalized["found"] is True
    assert normalized["inn"] == "5501234567"
    assert normalized["ogrn"] == "1155500001234"
    assert normalized["confidence"] == 0.92


def test_map_openai_fallback_to_registries_requires_high_confidence():
    result = CheckResult(
        registry="openai_company_fallback",
        found=True,
        details={
            "name_normalized": "ООО Тест",
            "entity_type": "ЮЛ",
            "inn": "5501234567",
            "ogrn": "1155500001234",
            "status": "active",
            "confidence": 0.9,
        },
    )
    mapped = map_openai_fallback_to_registries(result)
    assert mapped["dadata_fns"]["found"] is True
    assert mapped["dadata_fns"]["details"]["source"] == "openai_company_fallback"
