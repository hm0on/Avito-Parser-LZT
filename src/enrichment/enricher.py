"""Enrichment orchestrator — runs all registry checks and normalizes data."""

import asyncio
import uuid

import structlog

from src.collectors.base import RawReview
from src.config import settings
from src.database.models import CompanyEnriched, CompanyRaw
from src.enrichment.legal_bindings import LegalBindingSnapshot, decide_legal_match
from src.enrichment.normalizers import normalize_phones, parse_address
from src.enrichment.registries.arbitr import ArbitrChecker
from src.enrichment.registries.base import CheckResult
from src.enrichment.registries.dadata import DaDataChecker
from src.enrichment.registries.efrsb import EfrsbChecker
from src.enrichment.registries.eis import EisChecker
from src.enrichment.registries.fssp import FsspChecker
from src.enrichment.registries.listorg import (
    ListOrgChecker,
    is_listorg_complete,
    map_listorg_to_registries,
)
from src.enrichment.registries.openai_company_fallback import (
    OpenAICompanyFallbackChecker,
    map_openai_fallback_to_registries,
)
from src.enrichment.registries.nostroy import NostroyChecker
from src.enrichment.registries.rusprofile import (
    RusprofileChecker,
    map_rusprofile_to_registries,
)
from src.enrichment.review_searcher import ReviewSearcher
from src.enrichment.website_scanner import WebsiteScanner, extract_website_candidates

log = structlog.get_logger(__name__)

_LEGACY_CHECKERS_ALWAYS = [
    DaDataChecker(),
    ArbitrChecker(),
    EfrsbChecker(),
    EisChecker(),
    NostroyChecker(),
]

_FSSP_CHECKER = FsspChecker()

_FSSP_DISABLED_STUB = {
    "registry": "fssp",
    "found": False,
    "error": "disabled_by_config",
    "details": {},
}

_listorg_checker = ListOrgChecker()
_rusprofile_checker = RusprofileChecker()
_openai_company_fallback_checker = OpenAICompanyFallbackChecker()
_review_searcher = ReviewSearcher()
_website_scanner = WebsiteScanner()

# Points awarded for each data/check type toward confidence_score
_CONFIDENCE_WEIGHTS = {
    "has_inn": 20,
    "has_ogrn": 10,
    "has_phone": 10,
    "has_address": 5,
    "dadata_active": 25,
    "dadata_found": 15,
    "fssp_clean": 5,
    "nostroy_active": 10,
}


class Enricher:
    """Orchestrates enrichment of a single CompanyRaw record."""

    async def enrich(
        self,
        raw: CompanyRaw,
        *,
        existing_binding: LegalBindingSnapshot | None = None,
    ) -> tuple[CompanyEnriched, list[RawReview]]:
        log.info("enricher.start", raw_id=str(raw.id), name=raw.name_raw)
        avito_light_mode = bool(settings.avito_light_enrichment and raw.source == "avito")

        # 0. Website scan: save website, enrich contacts, try INN/OGRN extraction.
        await self._enrich_from_website(raw)

        # 1. Normalize phones
        raw_phones: list[str] = raw.phones or []
        phones_normalized = normalize_phones(raw_phones)

        # 2. Parse addresses
        raw_addresses: list[str] = raw.addresses or []
        addresses_parsed = [parse_address(a) for a in raw_addresses]

        # 3. Registry checks (INN/OGRN first; list-org name fallback if allowed)
        checks: dict[str, dict] = {}
        source_inn = raw.inn
        source_ogrn = raw.ogrn
        inn = raw.inn or (existing_binding.inn if existing_binding else None)
        ogrn = raw.ogrn or (existing_binding.ogrn if existing_binding else None)

        can_try_name_lookup = bool(settings.listorg_enabled and settings.listorg_allow_name_lookup and raw.name_raw)
        if avito_light_mode:
            checks = {
                "listorg": {
                    "registry": "listorg",
                    "found": False,
                    "status": None,
                    "details": {},
                    "error": "disabled_for_avito_light_mode",
                },
                "rusprofile": {
                    "registry": "rusprofile",
                    "found": False,
                    "status": None,
                    "details": {},
                    "error": "disabled_for_avito_light_mode",
                },
                "openai_company_fallback": {
                    "registry": "openai_company_fallback",
                    "found": False,
                    "status": None,
                    "details": {},
                    "error": "disabled_for_avito_light_mode",
                },
            }
            log.info("enricher.registry_checks.skipped", raw_id=str(raw.id), source=raw.source, mode="avito_light")
        elif inn or ogrn or can_try_name_lookup:
            checks = await self._run_registry_checks(inn=inn, ogrn=ogrn, name=raw.name_raw, raw_id=str(raw.id))
        else:
            log.info("enricher.skip_registry_checks", raw_id=str(raw.id), reason="no INN/OGRN")

        # 4. Determine normalized name and entity type from DaData result
        source_name_primary = (raw.name_raw or "").strip() or "Без названия"
        name_normalized = source_name_primary
        decision = decide_legal_match(
            raw,
            checks,
            source_inn=source_inn,
            source_ogrn=source_ogrn,
            existing_binding=existing_binding,
            avito_light_mode=avito_light_mode,
        )

        inn = decision.inn
        ogrn = decision.ogrn
        entity_type = decision.entity_type
        legal_name = decision.legal_name
        legal_match_method = decision.legal_match_method
        legal_match_score = decision.legal_match_score
        legal_verified = decision.legal_verified

        # 5. Compute confidence score
        confidence_score = self._compute_confidence(
            inn=inn,
            ogrn=ogrn,
            phones=phones_normalized,
            addresses=addresses_parsed,
            checks=checks,
        )
        manual_review_required = confidence_score < settings.confidence_threshold
        if decision.conflict:
            manual_review_required = True
        if settings.strict_legal_match and not legal_verified:
            manual_review_required = True

        relevance_details = {}
        relevance_score = None
        if isinstance(raw.raw_payload, dict):
            rel = raw.raw_payload.get("relevance")
            if isinstance(rel, dict):
                relevance_details = rel
                try:
                    relevance_score = float(rel.get("confidence"))
                except Exception:
                    relevance_score = None

        checks["legal_match"] = {
            "verified": legal_verified,
            "method": legal_match_method,
            "score": legal_match_score,
            "override_reason": "avito_light_mode" if avito_light_mode else None,
            "conflict": decision.conflict,
            "bindable": decision.bindable,
            "binding_strength": decision.binding_strength,
            "binding_source": decision.binding_source,
            "evidence": decision.evidence | {
                "inn": inn,
                "ogrn": ogrn,
                "legal_name": legal_name,
            },
        }

        log.info(
            "enricher.done",
            raw_id=str(raw.id),
            confidence=confidence_score,
            manual_review=manual_review_required,
            legal_verified=legal_verified,
        )

        # 6. Search additional reviews on Flamp, VK, Otzovik
        extra_reviews: list[RawReview] = []
        if avito_light_mode:
            log.info("enricher.extra_reviews.skipped", raw_id=str(raw.id), source=raw.source, mode="avito_light")
        else:
            try:
                extra_reviews = await _review_searcher.search(raw)
            except Exception as exc:
                log.error("enricher.extra_reviews.error", raw_id=str(raw.id), error=str(exc))

        return CompanyEnriched(
            id=uuid.uuid4(),
            raw_id=raw.id,
            inn=inn,
            ogrn=ogrn,
            name_normalized=name_normalized,
            entity_type=entity_type,
            phones_normalized=phones_normalized,
            addresses_parsed=addresses_parsed,
            checks=checks,
            legal_verified=legal_verified,
            legal_match_method=legal_match_method,
            legal_match_score=legal_match_score,
            relevance_score=relevance_score,
            relevance_details=relevance_details or None,
            confidence_score=confidence_score,
            manual_review_required=manual_review_required,
        ), extra_reviews

    async def _run_registry_checks(
        self, *, inn: str | None, ogrn: str | None, name: str | None, raw_id: str
    ) -> dict[str, dict]:
        """Run registry checks with list-org -> rusprofile -> OpenAI fallback strategy."""
        checks: dict[str, dict] = {}
        primary_found = False

        # Step 1: Try list-org as primary source
        listorg_result: CheckResult | None = None
        if settings.listorg_enabled and settings.listorg_primary:
            listorg_result = await _listorg_checker.safe_check(inn=inn, ogrn=ogrn, name=name)
            checks["listorg"] = listorg_result.to_dict()

            if listorg_result.found:
                primary_found = True
                mapped = map_listorg_to_registries(listorg_result)
                for reg_key, reg_dict in mapped.items():
                    checks[reg_key] = reg_dict

                # If list-org resolved legal entity details, reuse IDs for legacy checks.
                dadata_like = mapped.get("dadata_fns", {})
                details = dadata_like.get("details", {}) if isinstance(dadata_like, dict) else {}
                inn = inn or details.get("inn")
                ogrn = ogrn or details.get("ogrn")

                if is_listorg_complete(listorg_result) and not settings.listorg_fallback_enabled:
                    log.info(
                        "enricher.listorg_complete_skip_legacy",
                        raw_id=raw_id,
                        completeness=listorg_result.details.get("completeness"),
                    )
                    return checks

                log.info(
                    "enricher.listorg_partial_run_legacy",
                    raw_id=raw_id,
                    completeness=listorg_result.details.get("completeness"),
                )

        # Step 1b: If list-org did not find anything, try rusprofile.
        if not primary_found and settings.rusprofile_enabled:
            rusprofile_result = await _rusprofile_checker.safe_check(inn=inn, ogrn=ogrn, name=name)
            checks["rusprofile"] = rusprofile_result.to_dict()

            if rusprofile_result.found:
                primary_found = True
                mapped = map_rusprofile_to_registries(rusprofile_result)
                for reg_key, reg_dict in mapped.items():
                    existing = checks.get(reg_key)
                    if existing is None or not existing.get("found"):
                        checks[reg_key] = reg_dict
                dadata_like = mapped.get("dadata_fns", {})
                details = dadata_like.get("details", {}) if isinstance(dadata_like, dict) else {}
                inn = inn or details.get("inn")
                ogrn = ogrn or details.get("ogrn")

        # Step 1c: Last resort — OpenAI fallback hints.
        if not primary_found and settings.openai_company_fallback_enabled:
            openai_result = await _openai_company_fallback_checker.safe_check(inn=inn, ogrn=ogrn, name=name)
            checks["openai_company_fallback"] = openai_result.to_dict()
            mapped = map_openai_fallback_to_registries(openai_result)
            for reg_key, reg_dict in mapped.items():
                existing = checks.get(reg_key)
                if existing is None or not existing.get("found"):
                    checks[reg_key] = reg_dict
            if mapped:
                details = mapped.get("dadata_fns", {}).get("details", {})
                if isinstance(details, dict):
                    inn = inn or details.get("inn")
                    ogrn = ogrn or details.get("ogrn")

        if not inn and not ogrn:
            log.info(
                "enricher.skip_legacy_checks",
                raw_id=raw_id,
                reason="no INN/OGRN after fallback chain",
            )
            if not settings.enable_fssp_check and "fssp" not in checks:
                checks["fssp"] = dict(_FSSP_DISABLED_STUB)
            return checks

        # Step 2: Build legacy checker list (FSSP is conditional)
        legacy_checkers = list(_LEGACY_CHECKERS_ALWAYS)
        if settings.enable_fssp_check:
            legacy_checkers.append(_FSSP_CHECKER)

        results: list[CheckResult] = await asyncio.gather(
            *[checker.safe_check(inn=inn, ogrn=ogrn, name=name) for checker in legacy_checkers]
        )

        for result in results:
            # Legacy results override list-org if they provide richer data
            existing = checks.get(result.registry)
            if existing is None or not existing.get("found"):
                checks[result.registry] = result.to_dict()
            elif result.found:
                # Legacy found data too — prefer legacy for authoritative registries
                checks[result.registry] = result.to_dict()

        # If FSSP is disabled and not populated by list-org, add stable stub
        if not settings.enable_fssp_check and "fssp" not in checks:
            checks["fssp"] = dict(_FSSP_DISABLED_STUB)

        return checks

    async def _enrich_from_website(self, raw: CompanyRaw) -> None:
        websites = extract_website_candidates(raw)
        if not websites:
            return

        contacts = raw.contacts_json or {}
        existing_sites = contacts.get("websites") if isinstance(contacts.get("websites"), list) else []
        merged_sites = []
        for s in existing_sites + websites:
            if isinstance(s, str) and s not in merged_sites:
                merged_sites.append(s)
        contacts["websites"] = merged_sites

        # Scan first candidates; usually one corporate domain is enough.
        scan_results = []
        for site in merged_sites[:2]:
            try:
                res = await _website_scanner.scan(site)
                scan_results.append(res.to_dict())

                # Merge contacts
                raw_phones = raw.phones or []
                for p in res.phones:
                    if p not in raw_phones:
                        raw_phones.append(p)
                raw.phones = raw_phones

                raw_emails = raw.emails or []
                for e in res.emails:
                    if e not in raw_emails:
                        raw_emails.append(e)
                raw.emails = raw_emails

                if not raw.inn and res.inn:
                    raw.inn = res.inn
                if not raw.ogrn and res.ogrn:
                    raw.ogrn = res.ogrn
            except Exception as exc:
                log.warning("enricher.website_scan.error", raw_id=str(raw.id), site=site, error=str(exc))

        if scan_results:
            contacts["website_scan"] = scan_results
        raw.contacts_json = contacts

    def _compute_confidence(
        self,
        inn: str | None,
        ogrn: str | None,
        phones: list[str],
        addresses: list[dict],
        checks: dict,
    ) -> int:
        score = 0

        if inn:
            score += _CONFIDENCE_WEIGHTS["has_inn"]
        if ogrn:
            score += _CONFIDENCE_WEIGHTS["has_ogrn"]
        if phones:
            score += _CONFIDENCE_WEIGHTS["has_phone"]
        if addresses:
            score += _CONFIDENCE_WEIGHTS["has_address"]

        dd = checks.get("dadata_fns", {})
        if dd.get("found"):
            score += _CONFIDENCE_WEIGHTS["dadata_found"]
            if dd.get("status", "").lower() in ("active", "действующий"):
                score += _CONFIDENCE_WEIGHTS["dadata_active"]

        fssp = checks.get("fssp", {})
        if not fssp.get("found"):
            score += _CONFIDENCE_WEIGHTS["fssp_clean"]

        nostroy = checks.get("nostroy", {})
        if nostroy.get("found") and nostroy.get("status") == "active":
            score += _CONFIDENCE_WEIGHTS["nostroy_active"]

        return min(score, 100)
