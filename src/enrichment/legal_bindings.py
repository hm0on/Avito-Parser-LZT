from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.company_identity import build_identity_key
from src.config import settings
from src.database.models import CompanyLegalBinding, CompanyRaw

_NAME_TOKEN_RE = re.compile(r"[a-zа-я0-9]+", re.IGNORECASE)
_NAME_STOPWORDS = {
    "ооо",
    "ип",
    "ао",
    "пао",
    "зао",
    "оао",
    "мкооо",
    "гк",
    "фирма",
    "компания",
    "группа",
}
_NAME_LOOKUP_METHODS = {"name", "name_or_id"}


@dataclass
class LegalBindingSnapshot:
    identity_key: str
    source_name_snapshot: str | None
    legal_name: str | None
    inn: str | None
    ogrn: str | None
    binding_strength: str | None
    binding_source: str | None
    evidence_json: dict | None


@dataclass
class LegalMatchDecision:
    inn: str | None
    ogrn: str | None
    entity_type: str
    legal_name: str | None
    legal_verified: bool
    legal_match_method: str | None
    legal_match_score: float | None
    evidence: dict
    bindable: bool = False
    binding_strength: str | None = None
    binding_source: str | None = None
    conflict: bool = False


def tokenize_brand_name(value: str | None) -> set[str]:
    tokens = {
        token
        for token in _NAME_TOKEN_RE.findall((value or "").lower())
        if token not in _NAME_STOPWORDS
    }
    return tokens


def names_strictly_compatible(lhs: str | None, rhs: str | None) -> bool:
    left = tokenize_brand_name(lhs)
    right = tokenize_brand_name(rhs)
    if not left or not right:
        return False
    overlap = len(left & right)
    precision = overlap / max(1, len(left))
    recall = overlap / max(1, len(right))
    threshold = max(0.5, min(1.0, settings.legal_name_match_min_overlap))
    return precision >= threshold and recall >= threshold


def extract_binding_snapshot(row: CompanyLegalBinding | None) -> LegalBindingSnapshot | None:
    if row is None:
        return None
    return LegalBindingSnapshot(
        identity_key=row.identity_key,
        source_name_snapshot=row.source_name_snapshot,
        legal_name=row.legal_name,
        inn=row.inn,
        ogrn=row.ogrn,
        binding_strength=row.binding_strength,
        binding_source=row.binding_source,
        evidence_json=row.evidence_json,
    )


def _candidate_from_checks(checks: dict) -> dict:
    dd = checks.get("dadata_fns", {}) if isinstance(checks, dict) else {}
    details = dd.get("details", {}) if isinstance(dd, dict) else {}
    return {
        "found": bool(dd.get("found")),
        "source": details.get("source") or dd.get("registry"),
        "match_method": details.get("match_method") or details.get("source") or "registry",
        "match_score": details.get("match_score"),
        "name": details.get("name"),
        "inn": details.get("inn"),
        "ogrn": details.get("ogrn"),
        "entity_type": details.get("entity_type") or "unknown",
        "unverified": bool(details.get("unverified")),
    }


def _ids_match(expected_inn: str | None, expected_ogrn: str | None, candidate_inn: str | None, candidate_ogrn: str | None) -> bool:
    if expected_inn and candidate_inn and expected_inn != candidate_inn:
        return False
    if expected_ogrn and candidate_ogrn and expected_ogrn != candidate_ogrn:
        return False
    return bool((expected_inn and candidate_inn) or (expected_ogrn and candidate_ogrn))


def decide_legal_match(
    raw: CompanyRaw,
    checks: dict,
    *,
    source_inn: str | None,
    source_ogrn: str | None,
    existing_binding: LegalBindingSnapshot | None,
    avito_light_mode: bool,
) -> LegalMatchDecision:
    if avito_light_mode:
        return LegalMatchDecision(
            inn=source_inn,
            ogrn=source_ogrn,
            entity_type="unknown",
            legal_name=None,
            legal_verified=True,
            legal_match_method="avito_light_mode",
            legal_match_score=1.0,
            evidence={"override_reason": "avito_light_mode"},
            bindable=False,
        )

    candidate = _candidate_from_checks(checks)
    candidate_inn = candidate["inn"] or source_inn
    candidate_ogrn = candidate["ogrn"] or source_ogrn
    candidate_name = candidate["name"]
    candidate_entity_type = candidate["entity_type"] or "unknown"
    match_method = str(candidate["match_method"] or "registry")
    match_score = candidate["match_score"]
    source_has_ids = bool(source_inn or source_ogrn)
    candidate_name_ok = names_strictly_compatible(raw.name_raw, candidate_name) if candidate_name else False

    evidence = {
        "source_name": raw.name_raw,
        "candidate_name": candidate_name,
        "candidate_inn": candidate_inn,
        "candidate_ogrn": candidate_ogrn,
        "source_inn": source_inn,
        "source_ogrn": source_ogrn,
        "source_has_ids": source_has_ids,
        "candidate_name_ok": candidate_name_ok,
        "existing_binding": existing_binding.__dict__ if existing_binding else None,
    }

    if existing_binding and settings.legal_binding_memory_enabled:
        binding_name = existing_binding.legal_name or existing_binding.source_name_snapshot
        binding_name_ok = names_strictly_compatible(raw.name_raw, binding_name)
        binding_ids_conflict = (
            bool(candidate["found"])
            and (
                (existing_binding.inn and candidate_inn and existing_binding.inn != candidate_inn)
                or (existing_binding.ogrn and candidate_ogrn and existing_binding.ogrn != candidate_ogrn)
            )
        )
        evidence["binding_name_ok"] = binding_name_ok
        evidence["binding_ids_conflict"] = binding_ids_conflict
        if binding_name_ok and not binding_ids_conflict:
            return LegalMatchDecision(
                inn=existing_binding.inn or candidate_inn,
                ogrn=existing_binding.ogrn or candidate_ogrn,
                entity_type=candidate_entity_type,
                legal_name=existing_binding.legal_name or candidate_name,
                legal_verified=bool(existing_binding.inn or existing_binding.ogrn),
                legal_match_method="binding_memory",
                legal_match_score=1.0,
                evidence=evidence | {"binding_used": True},
                bindable=False,
                binding_strength=existing_binding.binding_strength,
                binding_source=existing_binding.binding_source,
            )
        if binding_ids_conflict:
            return LegalMatchDecision(
                inn=None,
                ogrn=None,
                entity_type=candidate_entity_type,
                legal_name=None,
                legal_verified=False,
                legal_match_method="binding_conflict",
                legal_match_score=None,
                evidence=evidence | {"rejected_reason": "binding_conflict"},
                bindable=False,
                conflict=True,
            )

    if not candidate["found"] or candidate["unverified"]:
        return LegalMatchDecision(
            inn=source_inn,
            ogrn=source_ogrn,
            entity_type=candidate_entity_type,
            legal_name=None,
            legal_verified=False,
            legal_match_method=None,
            legal_match_score=None,
            evidence=evidence | {"rejected_reason": "no_verified_registry_candidate"},
            bindable=False,
        )

    if source_has_ids and _ids_match(source_inn, source_ogrn, candidate_inn, candidate_ogrn):
        return LegalMatchDecision(
            inn=candidate_inn,
            ogrn=candidate_ogrn,
            entity_type=candidate_entity_type,
            legal_name=candidate_name,
            legal_verified=bool(candidate_inn or candidate_ogrn),
            legal_match_method=match_method,
            legal_match_score=float(match_score) if match_score is not None else 1.0,
            evidence=evidence | {"accepted_reason": "source_ids_confirmed"},
            bindable=True,
            binding_strength="source_ids",
            binding_source=match_method,
        )

    if match_method in _NAME_LOOKUP_METHODS and not settings.legal_auto_apply_name_lookup:
        return LegalMatchDecision(
            inn=None,
            ogrn=None,
            entity_type=candidate_entity_type,
            legal_name=None,
            legal_verified=False,
            legal_match_method=match_method,
            legal_match_score=float(match_score) if match_score is not None else None,
            evidence=evidence | {"rejected_reason": "name_lookup_requires_stronger_evidence"},
            bindable=False,
        )

    if candidate_name_ok and settings.legal_auto_apply_name_lookup:
        return LegalMatchDecision(
            inn=candidate_inn,
            ogrn=candidate_ogrn,
            entity_type=candidate_entity_type,
            legal_name=candidate_name,
            legal_verified=bool(candidate_inn or candidate_ogrn),
            legal_match_method=match_method,
            legal_match_score=float(match_score) if match_score is not None else None,
            evidence=evidence | {"accepted_reason": "explicit_name_lookup_enabled"},
            bindable=bool(candidate_inn or candidate_ogrn),
            binding_strength="name_lookup",
            binding_source=match_method,
        )

    return LegalMatchDecision(
        inn=None,
        ogrn=None,
        entity_type=candidate_entity_type,
        legal_name=None,
        legal_verified=False,
        legal_match_method=match_method,
        legal_match_score=float(match_score) if match_score is not None else None,
        evidence=evidence | {"rejected_reason": "insufficient_evidence"},
        bindable=False,
    )


async def load_legal_bindings(
    session: AsyncSession,
    *,
    identity_keys: list[str] | None = None,
) -> dict[str, LegalBindingSnapshot]:
    stmt = select(CompanyLegalBinding)
    if identity_keys:
        stmt = stmt.where(CompanyLegalBinding.identity_key.in_(identity_keys))
    rows = list((await session.execute(stmt)).scalars().all())
    out: dict[str, LegalBindingSnapshot] = {}
    for row in rows:
        snapshot = extract_binding_snapshot(row)
        if snapshot is not None:
            out[row.identity_key] = snapshot
    return out


async def upsert_legal_binding(
    session: AsyncSession,
    *,
    raw: CompanyRaw,
    decision: LegalMatchDecision,
) -> CompanyLegalBinding | None:
    if not decision.bindable or not settings.legal_binding_memory_enabled:
        return None

    identity_key = build_identity_key(raw)
    stmt = select(CompanyLegalBinding).where(CompanyLegalBinding.identity_key == identity_key)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = CompanyLegalBinding(identity_key=identity_key)
        session.add(row)

    row.source_name_snapshot = raw.name_raw
    row.legal_name = decision.legal_name
    row.inn = decision.inn
    row.ogrn = decision.ogrn
    row.binding_strength = decision.binding_strength
    row.binding_source = decision.binding_source
    row.evidence_json = decision.evidence
    await session.flush()
    return row
