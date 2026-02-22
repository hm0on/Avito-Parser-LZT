"""Enrichment orchestrator — runs all registry checks and normalizes data."""

import asyncio
import uuid

import structlog

from src.collectors.base import RawReview
from src.config import settings
from src.database.models import CompanyEnriched, CompanyRaw
from src.enrichment.normalizers import normalize_phones, parse_address
from src.enrichment.registries.arbitr import ArbitrChecker
from src.enrichment.registries.base import CheckResult
from src.enrichment.registries.dadata import DaDataChecker
from src.enrichment.registries.efrsb import EfrsbChecker
from src.enrichment.registries.eis import EisChecker
from src.enrichment.registries.fns_disqualified import FnsDisqualifiedChecker
from src.enrichment.registries.fns_mass_address import FnsMassAddressChecker
from src.enrichment.registries.fns_msp import FnsMspChecker
from src.enrichment.registries.fns_npd import FnsNpdChecker
from src.enrichment.registries.fns_pb import FnsPbChecker
from src.enrichment.registries.fssp import FsspChecker
from src.enrichment.registries.nostroy import NostroyChecker
from src.enrichment.registries.proverki import ProverkiChecker
from src.enrichment.registries.rnp import RnpChecker
from src.enrichment.review_searcher import ReviewSearcher
from src.enrichment.website_scanner import WebsiteScanner, extract_website_candidates

log = structlog.get_logger(__name__)

# Pass 1: checkers that only need INN/OGRN/name
_PASS1_CHECKERS = [
    DaDataChecker(),
    FnsPbChecker(),
    FsspChecker(),
    ArbitrChecker(),
    EfrsbChecker(),
    EisChecker(),
    NostroyChecker(),
    FnsMspChecker(),
    RnpChecker(),
    ProverkiChecker(),
    FnsNpdChecker(),
]

# Pass 2: checkers that need data from pass 1 (director FIO, legal address)
_PASS2_CHECKERS = [
    FnsDisqualifiedChecker(),
    FnsMassAddressChecker(),
]

_review_searcher = ReviewSearcher()
_website_scanner = WebsiteScanner()

# Points awarded for each data/check type toward confidence_score
_CONFIDENCE_WEIGHTS = {
    # Identity completeness (30 pts max)
    "has_inn": 15,
    "has_ogrn": 8,
    "has_phone": 5,
    "has_address": 2,
    # DaData / FNS registration (25 pts max)
    "dadata_found": 10,
    "dadata_active": 15,
    # Clean registry checks (35 pts max)
    "fssp_clean": 5,
    "nostroy_active": 8,
    "rnp_clean": 7,
    "msp_found": 3,
    "disqualification_clean": 5,
    "mass_address_clean": 4,
    "efrsb_clean": 3,
    # Bonus (10 pts max)
    "fns_pb_no_risks": 5,
    "proverki_clean": 2,
    "npd_confirmed": 2,
}


class Enricher:
    """Orchestrates enrichment of a single CompanyRaw record."""

    async def enrich(self, raw: CompanyRaw) -> tuple[CompanyEnriched, list[RawReview]]:
        log.info("enricher.start", raw_id=str(raw.id), name=raw.name_raw)

        # 0. Website scan: extract INN/OGRN/phones/emails from company websites.
        await self._enrich_from_website(raw)

        # 1. Normalize phones
        raw_phones: list[str] = raw.phones or []
        phones_normalized = normalize_phones(raw_phones)

        # 2. Parse addresses
        raw_addresses: list[str] = raw.addresses or []
        addresses_parsed = [parse_address(a) for a in raw_addresses]

        # 3. Registry checks — Pass 1 (only if INN/OGRN available)
        checks: dict[str, dict] = {}
        inn = raw.inn
        ogrn = raw.ogrn

        if inn or ogrn:
            results: list[CheckResult] = await asyncio.gather(
                *[checker.safe_check(inn=inn, ogrn=ogrn, name=raw.name_raw) for checker in _PASS1_CHECKERS]
            )
            for result in results:
                checks[result.registry] = result.to_dict()
        else:
            log.info("enricher.skip_registry_checks", raw_id=str(raw.id), reason="no INN/OGRN")

        # 4. Determine normalized name and entity type from DaData result
        name_normalized = raw.name_raw
        entity_type = "unknown"
        if "dadata_fns" in checks and checks["dadata_fns"]["found"]:
            dd = checks["dadata_fns"]["details"]
            name_normalized = dd.get("name") or name_normalized
            entity_type = dd.get("entity_type", "unknown")
            inn = inn or dd.get("inn")
            ogrn = ogrn or dd.get("ogrn")

        # 5. Registry checks — Pass 2 (needs director FIO and address from pass 1)
        if inn or ogrn:
            director_fio = self._extract_director_fio(checks)
            legal_address = self._extract_legal_address(checks)

            if director_fio or legal_address:
                extra = {"director_fio": director_fio, "legal_address": legal_address}
                pass2_results: list[CheckResult] = await asyncio.gather(
                    *[checker.safe_check(inn=inn, ogrn=ogrn, name=raw.name_raw, extra=extra)
                      for checker in _PASS2_CHECKERS]
                )
                for result in pass2_results:
                    checks[result.registry] = result.to_dict()

        # 6. Compute confidence score
        confidence_score = self._compute_confidence(
            inn=inn,
            ogrn=ogrn,
            phones=phones_normalized,
            addresses=addresses_parsed,
            checks=checks,
        )
        manual_review_required = confidence_score < settings.confidence_threshold

        log.info(
            "enricher.done",
            raw_id=str(raw.id),
            confidence=confidence_score,
            manual_review=manual_review_required,
            checks_count=len(checks),
        )

        # 7. Search additional reviews on Flamp, VK, Otzovik
        extra_reviews: list[RawReview] = []
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
            confidence_score=confidence_score,
            manual_review_required=manual_review_required,
        ), extra_reviews

    def _extract_director_fio(self, checks: dict) -> str:
        """Extract director FIO from FNS PB or DaData results."""
        pb = checks.get("fns_pb", {})
        if pb.get("found"):
            fio = pb.get("details", {}).get("head_fio", "")
            if fio:
                return fio

        dd = checks.get("dadata_fns", {})
        if dd.get("found"):
            # For ИП, the company name IS the FIO
            if dd.get("details", {}).get("entity_type") == "ИП":
                return dd.get("details", {}).get("name", "")

        return ""

    def _extract_legal_address(self, checks: dict) -> str:
        """Extract legal address from FNS PB or DaData results."""
        pb = checks.get("fns_pb", {})
        if pb.get("found"):
            addr = pb.get("details", {}).get("address", "")
            if addr:
                return addr

        dd = checks.get("dadata_fns", {})
        if dd.get("found"):
            return dd.get("details", {}).get("address", "")

        return ""

    async def _enrich_from_website(self, raw: CompanyRaw) -> None:
        """Scan company websites to extract INN, OGRN, phones, emails."""
        websites = extract_website_candidates(raw)
        if not websites:
            return

        contacts = raw.contacts_json or {}
        existing_sites = contacts.get("websites") if isinstance(contacts.get("websites"), list) else []
        merged_sites: list[str] = []
        for s in existing_sites + websites:
            if isinstance(s, str) and s not in merged_sites:
                merged_sites.append(s)
        contacts["websites"] = merged_sites

        scan_results = []
        for site in merged_sites[:2]:
            try:
                res = await _website_scanner.scan(site)
                scan_results.append(res.to_dict())

                # Merge phones
                raw_phones = raw.phones or []
                for p in res.phones:
                    if p not in raw_phones:
                        raw_phones.append(p)
                raw.phones = raw_phones

                # Merge emails
                raw_emails = raw.emails or []
                for e in res.emails:
                    if e not in raw_emails:
                        raw_emails.append(e)
                raw.emails = raw_emails

                # Extract INN/OGRN from website
                if not raw.inn and res.inn:
                    raw.inn = res.inn
                    log.info("enricher.website_inn_found", raw_id=str(raw.id), inn=res.inn, site=site)
                if not raw.ogrn and res.ogrn:
                    raw.ogrn = res.ogrn
                    log.info("enricher.website_ogrn_found", raw_id=str(raw.id), ogrn=res.ogrn, site=site)
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

        # DaData
        dd = checks.get("dadata_fns", {})
        if dd.get("found"):
            score += _CONFIDENCE_WEIGHTS["dadata_found"]
            if dd.get("status", "").lower() in ("active", "действующий"):
                score += _CONFIDENCE_WEIGHTS["dadata_active"]

        # FSSP clean (no enforcement proceedings)
        fssp = checks.get("fssp", {})
        if not fssp.get("found"):
            score += _CONFIDENCE_WEIGHTS["fssp_clean"]

        # NOSTROY active SRO membership
        nostroy = checks.get("nostroy", {})
        if nostroy.get("found") and nostroy.get("status") == "active":
            score += _CONFIDENCE_WEIGHTS["nostroy_active"]

        # RNP clean (NOT in dishonest suppliers registry)
        rnp = checks.get("rnp", {})
        if not rnp.get("found") or not rnp.get("details", {}).get("in_rnp"):
            score += _CONFIDENCE_WEIGHTS["rnp_clean"]

        # MSP found (in SME registry)
        msp = checks.get("fns_msp", {})
        if msp.get("found") and msp.get("details", {}).get("in_msp"):
            score += _CONFIDENCE_WEIGHTS["msp_found"]

        # Director not disqualified
        disq = checks.get("fns_disqualified", {})
        if disq.get("found") and not disq.get("details", {}).get("disqualified"):
            score += _CONFIDENCE_WEIGHTS["disqualification_clean"]

        # Address not mass-registration
        mass = checks.get("fns_mass_address", {})
        if mass.get("found") and not mass.get("details", {}).get("is_mass_address"):
            score += _CONFIDENCE_WEIGHTS["mass_address_clean"]

        # EFRSB clean (not bankrupt)
        efrsb = checks.get("efrsb", {})
        if not efrsb.get("found") or efrsb.get("status") != "bankrupt":
            score += _CONFIDENCE_WEIGHTS["efrsb_clean"]

        # FNS PB no risk markers
        pb = checks.get("fns_pb", {})
        if pb.get("found") and not pb.get("details", {}).get("has_risk_markers"):
            score += _CONFIDENCE_WEIGHTS["fns_pb_no_risks"]

        # Inspections clean (no violations)
        proverki = checks.get("proverki", {})
        if not proverki.get("found") or proverki.get("details", {}).get("with_violations", 0) == 0:
            score += _CONFIDENCE_WEIGHTS["proverki_clean"]

        # NPD confirmed (for self-employed)
        npd = checks.get("fns_npd", {})
        if npd.get("found") and npd.get("details", {}).get("is_npd"):
            score += _CONFIDENCE_WEIGHTS["npd_confirmed"]

        return min(score, 100)
