from __future__ import annotations

import uuid
from datetime import date

from src.database.models import CompanyRaw
from src.enrichment.legal_bindings import (
    LegalBindingSnapshot,
    decide_legal_match,
    names_strictly_compatible,
)


def _raw(name: str, *, inn: str | None = None, ogrn: str | None = None) -> CompanyRaw:
    return CompanyRaw(
        id=uuid.uuid4(),
        source="2gis",
        source_id=str(uuid.uuid4()),
        name_raw=name,
        inn=inn,
        ogrn=ogrn,
        collection_week_start=date(2026, 3, 2),
    )


def test_names_strictly_compatible_accepts_same_brand_tokens():
    assert names_strictly_compatible("Аква-Омск", "Аква Омск")


def test_names_strictly_compatible_rejects_generic_subset_name():
    assert not names_strictly_compatible("Аква", "Аква Омск")
    assert not names_strictly_compatible("Аква Омск", "Аква")


def test_decide_legal_match_rejects_name_only_candidate_for_similar_name():
    raw = _raw("Аква")
    checks = {
        "dadata_fns": {
            "found": True,
            "details": {
                "name": "Аква Омск",
                "inn": "5501000001",
                "ogrn": "1155500000001",
                "entity_type": "ЮЛ",
                "source": "listorg",
                "match_method": "name",
                "match_score": 0.8,
            },
        }
    }

    decision = decide_legal_match(
        raw,
        checks,
        source_inn=None,
        source_ogrn=None,
        existing_binding=None,
        avito_light_mode=False,
    )

    assert decision.legal_verified is False
    assert decision.inn is None
    assert decision.ogrn is None
    assert decision.evidence["rejected_reason"] == "name_lookup_requires_stronger_evidence"


def test_decide_legal_match_accepts_source_ids_confirmed_by_registry():
    raw = _raw("Аква Омск", inn="5501000001", ogrn="1155500000001")
    checks = {
        "dadata_fns": {
            "found": True,
            "details": {
                "name": "Аква Омск",
                "inn": "5501000001",
                "ogrn": "1155500000001",
                "entity_type": "ЮЛ",
                "source": "listorg",
                "match_method": "inn",
                "match_score": 1.0,
            },
        }
    }

    decision = decide_legal_match(
        raw,
        checks,
        source_inn=raw.inn,
        source_ogrn=raw.ogrn,
        existing_binding=None,
        avito_light_mode=False,
    )

    assert decision.legal_verified is True
    assert decision.inn == "5501000001"
    assert decision.binding_strength == "source_ids"
    assert decision.bindable is True


def test_decide_legal_match_reuses_existing_binding_for_same_source_card():
    raw = _raw("Аква Омск")
    binding = LegalBindingSnapshot(
        identity_key="2gis:id:test",
        source_name_snapshot="Аква Омск",
        legal_name="ООО Аква Омск",
        inn="5501000001",
        ogrn="1155500000001",
        binding_strength="source_ids",
        binding_source="inn",
        evidence_json={"accepted_reason": "source_ids_confirmed"},
    )

    decision = decide_legal_match(
        raw,
        checks={},
        source_inn=None,
        source_ogrn=None,
        existing_binding=binding,
        avito_light_mode=False,
    )

    assert decision.legal_verified is True
    assert decision.legal_match_method == "binding_memory"
    assert decision.inn == "5501000001"
