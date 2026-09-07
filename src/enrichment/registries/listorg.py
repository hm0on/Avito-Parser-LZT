"""List-org.com registry checker — primary enrichment source.

Scrapes list-org.com for a given INN/OGRN and extracts normalized signals
that map into the existing registry schema (fssp, kad_arbitr, efrsb,
eis_zakupki, nostroy, dadata_fns) to avoid downstream refactors.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import structlog
from bs4 import BeautifulSoup

from src.config import settings
from src.enrichment.registries.base import AbstractChecker, CheckResult
from src.proxy import enrichment_proxy_manager, iter_proxy_urls

log = structlog.get_logger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*",
}

_LISTORG_SEARCH_URL = "https://www.list-org.com/search"

# Signals we extract and the registry keys they map to
_REGISTRY_MAP = {
    "fssp": "fssp",
    "arbitr": "kad_arbitr",
    "bankrupt": "efrsb",
    "zakupki": "eis_zakupki",
    "sro": "nostroy",
    "legal_status": "dadata_fns",
}


class ListOrgChecker(AbstractChecker):
    """Checks list-org.com for company data by INN/OGRN.

    Returns a composite CheckResult whose details contain sub-results
    for each mapped registry signal.
    """

    registry_name = "listorg"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
    ) -> CheckResult:
        if not settings.listorg_enabled:
            return CheckResult(registry=self.registry_name, found=False, error="listorg disabled")

        id_candidates: list[tuple[str, str]] = []
        if inn:
            id_candidates.append(("inn", inn))
        if ogrn:
            id_candidates.append(("ogrn", ogrn))

        # Step 1: strict ID lookups (INN/OGRN)
        had_reachable_response = False
        for search_type, query in id_candidates:
            fetch_result = await self._fetch_page(query, search_type=search_type, match_name=name or "")
            if fetch_result is None:
                continue
            had_reachable_response = True
            html, name_match_score = fetch_result

            if _is_bot_page(html):
                continue

            parsed = self._parse_page(
                html,
                query,
                search_type=search_type,
                name_match_score=name_match_score,
            )
            if parsed.found:
                return parsed

        # Step 2: optional name fallback (if allowed)
        if settings.listorg_allow_name_lookup and name:
            query = _prepare_name_query(name)
            fetch_result = await self._fetch_page(
                query,
                search_type="name",
                match_name=name or "",
            )
            if fetch_result is None:
                if had_reachable_response:
                    return CheckResult(registry=self.registry_name, found=False)
                return CheckResult(registry=self.registry_name, found=False, error="all fetch attempts failed")

            had_reachable_response = True
            html, name_match_score = fetch_result
            if _is_bot_page(html):
                return CheckResult(
                    registry=self.registry_name,
                    found=False,
                    error="bot_page_detected",
                    details={"blocked": True},
                )
            return self._parse_page(
                html,
                query,
                search_type="name",
                name_match_score=name_match_score,
            )

        if had_reachable_response:
            return CheckResult(registry=self.registry_name, found=False)

        if not (settings.listorg_allow_name_lookup and name):
            return CheckResult(
                registry=self.registry_name,
                found=False,
                error="all fetch attempts failed" if id_candidates else "no INN/OGRN provided and name lookup disabled",
            )

        return CheckResult(registry=self.registry_name, found=False, error="all fetch attempts failed")

    async def _fetch_page(
        self,
        query: str,
        *,
        search_type: str,
        match_name: str,
    ) -> tuple[str, float | None] | None:
        timeout = max(5, settings.listorg_timeout_seconds)
        max_attempts = max(1, settings.listorg_max_attempts)
        rps_delay = 1.0 / max(0.1, settings.listorg_rate_limit_rps)

        if settings.listorg_use_enrichment_proxy and enrichment_proxy_manager.count > 0:
            proxy_candidates: list[str | None] = list(
                iter_proxy_urls(
                    enrichment_proxy_manager,
                    purpose="listorg registry",
                    max_attempts=max_attempts,
                )
            )
            # If all proxy attempts fail (e.g. 407), do one direct attempt.
            proxy_candidates.append(None)
        else:
            proxy_candidates = [None] * max_attempts

        last_error = ""
        attempts_done = 0

        for proxy_url in proxy_candidates:
            attempts_done += 1
            try:
                async with httpx.AsyncClient(
                    timeout=float(timeout),
                    headers=_BROWSER_HEADERS,
                    proxy=proxy_url,
                    follow_redirects=True,
                ) as client:
                    # Step 1: search page
                    resp = await client.get(
                        _LISTORG_SEARCH_URL,
                        params={"type": search_type, "val": query},
                    )
                    if resp.status_code in (403, 407, 429, 503):
                        last_error = f"HTTP {resp.status_code}"
                        log.warning(
                            "listorg.fetch.blocked",
                            status=resp.status_code,
                            proxy=proxy_url or "direct",
                        )
                        await asyncio.sleep(rps_delay)
                        continue
                    resp.raise_for_status()

                    # Check if we landed on a company page or search results
                    html = resp.text
                    if _is_company_page(html):
                        return html, None

                    # Follow result links if it's a search results page
                    company_urls = _extract_result_urls(html, limit=5)
                    if not company_urls:
                        log.info("listorg.search.not_found", query=query)
                        return html, None  # Return search page, will be parsed as not-found

                    # For name-based lookup, choose the best-matching card title.
                    best_html: str | None = None
                    best_score = -1.0

                    for company_url in company_urls:
                        resp2 = await client.get(company_url)
                        if resp2.status_code in (403, 407, 429, 503):
                            last_error = f"HTTP {resp2.status_code} on company page"
                            continue
                        resp2.raise_for_status()
                        company_html = resp2.text
                        if not _is_company_page(company_html):
                            continue
                        if search_type != "name":
                            return company_html, None

                        cand_name = _extract_company_name(BeautifulSoup(company_html, "html.parser")) or ""
                        score = _name_match_score(match_name, cand_name)
                        if score > best_score:
                            best_score = score
                            best_html = company_html

                    if best_html:
                        threshold = max(0.0, min(1.0, settings.listorg_name_match_min_score))
                        if best_score < threshold:
                            log.info(
                                "listorg.fetch.name_rejected",
                                query=query,
                                score=round(best_score, 3),
                                threshold=threshold,
                            )
                            return html, best_score
                        log.info(
                            "listorg.fetch.name_selected",
                            query=query,
                            score=round(best_score, 3),
                        )
                        return best_html, best_score

            except Exception as exc:
                last_error = str(exc)
                log.warning(
                    "listorg.fetch.error",
                    query=query,
                    error=last_error[:200],
                    proxy=proxy_url or "direct",
                )
                await asyncio.sleep(rps_delay)

        log.error("listorg.fetch.exhausted", query=query, attempts=attempts_done, last_error=last_error[:200])
        return None

    def _parse_page(
        self,
        html: str,
        query: str,
        *,
        search_type: str,
        name_match_score: float | None,
    ) -> CheckResult:
        soup = BeautifulSoup(html, "html.parser")

        # Check if company was found
        if not _is_company_page(html):
            return CheckResult(registry=self.registry_name, found=False)

        details: dict = {}
        signals: dict = {}

        # Extract company name and status
        name = _extract_company_name(soup)
        status = _extract_legal_status(soup)
        inn_found = _extract_field(soup, "ИНН")
        ogrn_found = _extract_field(soup, "ОГРН")

        details["name"] = name
        details["status"] = status
        details["inn"] = inn_found
        details["ogrn"] = ogrn_found
        details["match_method"] = search_type
        details["match_score"] = name_match_score
        director_name, director_position = _extract_director_info(soup)
        founders = _extract_founders(soup)
        details["director_name"] = director_name
        details["director_position"] = director_position
        details["founders"] = founders

        # Legal status signal → maps to dadata_fns
        signals["legal_status"] = {
            "found": True,
            "status": status,
            "details": {
                "name": name,
                "inn": inn_found,
                "ogrn": ogrn_found,
                "entity_type": _detect_entity_type(soup),
                "source": "listorg",
                "match_method": search_type,
                "match_score": name_match_score,
                "director_name": director_name,
                "director_position": director_position,
                "founders": founders,
            },
        }

        # Extract FSSP/execution signals
        fssp_data = _extract_fssp_signals(soup)
        signals["fssp"] = fssp_data

        # Extract arbitration signals
        arbitr_data = _extract_arbitr_signals(soup)
        signals["arbitr"] = arbitr_data

        # Extract bankruptcy signals
        bankrupt_data = _extract_bankrupt_signals(soup, status)
        signals["bankrupt"] = bankrupt_data

        # Extract zakupki signals
        zakupki_data = _extract_zakupki_signals(soup)
        signals["zakupki"] = zakupki_data

        # Extract SRO/NOSTROY signals
        sro_data = _extract_sro_signals(soup)
        signals["sro"] = sro_data

        # Assess completeness
        completeness = _assess_completeness(signals)
        details["signals"] = signals
        details["completeness"] = completeness

        log.info(
            "listorg.parse.done",
            query=query,
            name=name,
            status=status,
            completeness=completeness,
        )

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status=status,
            details=details,
        )

    def map_to_registry_results(self) -> dict[str, CheckResult]:
        """Convert listorg composite result into individual registry-compatible results."""
        # This is called by the enricher to decide whether to skip legacy checkers
        raise NotImplementedError("Use map_listorg_to_registries() standalone function")


def map_listorg_to_registries(listorg_result: CheckResult) -> dict[str, dict]:
    """Convert a listorg CheckResult into per-registry dicts compatible with risk_assessor.

    Returns dict mapping registry_name -> check dict (same format as CheckResult.to_dict()).
    """
    if not listorg_result.found:
        return {}

    signals = listorg_result.details.get("signals", {})
    mapped: dict[str, dict] = {}

    # Legal status → dadata_fns
    legal = signals.get("legal_status", {})
    if legal.get("found"):
        status_raw = (legal.get("status") or "").lower()
        dadata_status = "ACTIVE"
        if any(kw in status_raw for kw in ("ликвидир", "прекращ", "исключ")):
            dadata_status = "liquidated"
        elif "банкрот" in status_raw:
            dadata_status = "bankrupt"
        mapped["dadata_fns"] = {
            "registry": "dadata_fns",
            "found": True,
            "status": dadata_status,
            "details": legal.get("details", {}),
            "error": None,
        }

    # FSSP
    fssp = signals.get("fssp", {})
    if fssp.get("found"):
        mapped["fssp"] = {
            "registry": "fssp",
            "found": True,
            "status": "has_executions",
            "details": fssp.get("details", {}),
            "error": None,
        }
    else:
        mapped["fssp"] = {
            "registry": "fssp",
            "found": False,
            "status": None,
            "details": fssp.get("details", {}),
            "error": None,
        }

    # Arbitration
    arbitr = signals.get("arbitr", {})
    if arbitr.get("found"):
        mapped["kad_arbitr"] = {
            "registry": "kad_arbitr",
            "found": True,
            "status": "has_cases",
            "details": arbitr.get("details", {}),
            "error": None,
        }

    # Bankruptcy
    bankrupt = signals.get("bankrupt", {})
    if bankrupt.get("found"):
        mapped["efrsb"] = {
            "registry": "efrsb",
            "found": True,
            "status": bankrupt.get("status", "mentioned"),
            "details": bankrupt.get("details", {}),
            "error": None,
        }

    # Zakupki
    zakupki = signals.get("zakupki", {})
    if zakupki.get("found"):
        mapped["eis_zakupki"] = {
            "registry": "eis_zakupki",
            "found": True,
            "status": "participant",
            "details": zakupki.get("details", {}),
            "error": None,
        }

    # SRO/NOSTROY
    sro = signals.get("sro", {})
    if sro.get("found"):
        mapped["nostroy"] = {
            "registry": "nostroy",
            "found": True,
            "status": "active" if sro.get("active") else "inactive",
            "details": sro.get("details", {}),
            "error": None,
        }

    return mapped


def is_listorg_complete(listorg_result: CheckResult) -> bool:
    """Check if list-org data is complete enough to skip legacy checkers."""
    if not listorg_result.found:
        return False
    completeness = listorg_result.details.get("completeness", 0)
    return completeness >= 0.6


# ── HTML parsing helpers ──────────────────────────────────────────────────


def _prepare_name_query(name: str) -> str:
    query = (name or "").strip()
    region = settings.region.strip()
    if region and region.lower() not in query.lower():
        return f"{query} {region}".strip()
    return query


def _tokenize_name(value: str) -> set[str]:
    tokens = set(re.findall(r"[a-zа-я0-9]+", (value or "").lower(), flags=re.IGNORECASE))
    return {
        t
        for t in tokens
        if t not in {"ооо", "ип", "ао", "пао", "зао", "оао", "компания", "г", "город"}
    }


def _name_match_score(search_name: str, candidate_name: str) -> float:
    lhs = _tokenize_name(search_name)
    rhs = _tokenize_name(candidate_name)
    if not lhs or not rhs:
        return 0.0
    overlap = len(lhs & rhs)
    return overlap / max(1, len(lhs))


def _is_bot_page(html: str) -> bool:
    low = html.lower()
    return any(marker in low for marker in [
        "captcha",
        "access denied",
        "robot",
        "cloudflare",
        "please verify",
    ])


def _is_company_page(html: str) -> bool:
    if not html:
        return False
    low = html.lower()
    title_match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title_text = title_match.group(1).strip().lower() if title_match else ""
    if "список организаций" in title_text:
        return False

    has_inn = re.search(r"(?:инн|\"inn\")\D{0,20}(\d{10}|\d{12})", html, flags=re.IGNORECASE)
    has_ogrn = re.search(r"(?:огрн|огрнип|\"ogrn\"|\"ogrnip\")\D{0,20}(\d{13}|\d{15})", html, flags=re.IGNORECASE)
    has_h1 = "<h1" in low
    return bool(has_inn and has_ogrn and (has_h1 or "/company/" in low))


def _extract_first_result_url(html: str) -> str | None:
    urls = _extract_result_urls(html, limit=1)
    return urls[0] if urls else None


def _extract_result_urls(html: str, *, limit: int = 5) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.select("a[href*='/company/']"):
        href = a.get("href", "")
        if href:
            if not href.startswith("http"):
                href = f"https://www.list-org.com{href}"
            if href not in seen:
                seen.add(href)
                out.append(href)
        if len(out) >= max(1, limit):
            break
    return out


def _extract_company_name(soup: BeautifulSoup) -> str | None:
    h1 = soup.select_one("h1")
    if h1:
        return h1.get_text(strip=True)
    title = soup.select_one("title")
    if title:
        text = title.get_text(strip=True)
        # Remove trailing site name
        return text.split(" — ")[0].strip() if " — " in text else text
    return None


def _extract_legal_status(soup: BeautifulSoup) -> str:
    text = soup.get_text(" ", strip=True).lower()
    if any(kw in text for kw in ("действующее", "действующая", "действующий")):
        return "active"
    if any(kw in text for kw in ("ликвидировано", "ликвидирована", "прекращено")):
        return "liquidated"
    if "банкрот" in text:
        return "bankrupt"
    if "исключен" in text:
        return "liquidated"
    return "unknown"


def _extract_field(soup: BeautifulSoup, label: str) -> str | None:
    """Extract a field value from a label:value pattern on the page."""
    for el in soup.find_all(string=re.compile(re.escape(label), re.IGNORECASE)):
        parent = el.parent if el.parent else None
        if parent:
            text = parent.get_text(" ", strip=True)
            # Try to extract value after label
            match = re.search(rf"{re.escape(label)}\s*[:\s]\s*(\S+)", text)
            if match:
                return match.group(1).strip()
    return None


def _detect_entity_type(soup: BeautifulSoup) -> str:
    text = soup.get_text(" ", strip=True)
    if re.search(r"\bИП\b", text):
        return "ИП"
    if any(kw in text for kw in ("ООО", "АО", "ПАО", "ЗАО")):
        return "ЮЛ"
    if "ОГРНИП" in text:
        return "ИП"
    return "unknown"


def _extract_fssp_signals(soup: BeautifulSoup) -> dict:
    """Look for execution/FSSP-related signals on the page."""
    text = soup.get_text(" ", strip=True).lower()
    found = False
    details: dict = {"total_debt_rub": 0, "executions_count": 0}

    # Look for ФССП / исполнительные производства section
    if any(kw in text for kw in ("исполнительн", "фссп", "судебн")):
        found = True
        # Try to extract debt amount
        debt_match = re.search(
            r"(?:сумм\w*|задолженност\w*|долг\w*)\s*[:\s—]*\s*([\d\s,.]+)\s*(?:руб|₽|рублей)",
            text,
        )
        if debt_match:
            raw = debt_match.group(1).replace(" ", "").replace(",", ".").replace("\xa0", "")
            try:
                details["total_debt_rub"] = float(raw)
            except ValueError:
                pass

        # Try to extract count
        count_match = re.search(
            r"(?:исполнительн\S*\s+производств\S*|фссп)\s*[:\s—]*\s*(\d+)",
            text,
        )
        if count_match:
            details["executions_count"] = int(count_match.group(1))

    return {"found": found, "details": details}


def _extract_arbitr_signals(soup: BeautifulSoup) -> dict:
    """Look for arbitration court signals."""
    text = soup.get_text(" ", strip=True).lower()
    found = False
    details: dict = {"total_count": 0, "active_cases_count": 0}

    if any(kw in text for kw in ("арбитраж", "судебн", "дел в")):
        found = True
        count_match = re.search(
            r"(?:арбитраж\S*\s+дел\S*|дел\S*\s+в\s+суд\S*)\s*[:\s—]*\s*(\d+)",
            text,
        )
        if count_match:
            details["total_count"] = int(count_match.group(1))
            # Assume ~30% are active as conservative estimate
            details["active_cases_count"] = max(1, details["total_count"] // 3)

    return {"found": found, "details": details}


def _extract_bankrupt_signals(soup: BeautifulSoup, status: str) -> dict:
    """Look for bankruptcy signals."""
    text = soup.get_text(" ", strip=True).lower()
    found = False
    bankrupt_status = "mentioned"

    if "банкрот" in text or status == "bankrupt":
        found = True
        bankrupt_status = "bankrupt"
    elif any(kw in text for kw in ("ефрсб", "несостоятельн")):
        found = True

    return {"found": found, "status": bankrupt_status, "details": {}}


def _extract_zakupki_signals(soup: BeautifulSoup) -> dict:
    """Look for government procurement signals."""
    text = soup.get_text(" ", strip=True).lower()
    found = any(kw in text for kw in ("закупк", "госконтракт", "44-фз", "223-фз", "контрактн"))
    return {"found": found, "details": {}}


def _extract_sro_signals(soup: BeautifulSoup) -> dict:
    """Look for SRO membership signals."""
    text = soup.get_text(" ", strip=True).lower()
    found = any(kw in text for kw in ("сро", "нострой", "саморегулируем"))
    active = found  # If mentioned, assume active

    return {"found": found, "active": active, "details": {}}


def _assess_completeness(signals: dict) -> float:
    """Return a 0.0-1.0 completeness score for listorg data.

    Key signals: legal_status is mandatory. Each additional signal adds weight.
    """
    if not signals.get("legal_status", {}).get("found"):
        return 0.0

    score = 0.3  # Base: legal status found
    weights = {
        "fssp": 0.15,
        "arbitr": 0.15,
        "bankrupt": 0.1,
        "zakupki": 0.1,
        "sro": 0.2,
    }
    for key, weight in weights.items():
        signal = signals.get(key, {})
        if signal.get("found") is not None:  # Signal was checked (even if not found)
            score += weight

    return min(score, 1.0)


def _extract_director_info(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    text = soup.get_text(" ", strip=True)
    patterns = [
        r"(Генеральн\w+\s+директор)\s*[:\-]?\s*([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z .-]{3,120}?)"
        r"(?=\s+(?:Учредител|Участник|ИНН|ОГРН|Статус|$))",
        r"(Директор)\s*[:\-]?\s*([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z .-]{3,120}?)"
        r"(?=\s+(?:Учредител|Участник|ИНН|ОГРН|Статус|$))",
        r"(Руководитель)\s*[:\-]?\s*([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z .-]{3,120}?)"
        r"(?=\s+(?:Учредител|Участник|ИНН|ОГРН|Статус|$))",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            position = " ".join(match.group(1).split())
            name = " ".join(match.group(2).split()).strip(" .,;:-")
            return name, position
    return None, None


def _extract_founders(soup: BeautifulSoup) -> list[str]:
    text = soup.get_text(" ", strip=True)
    found: list[str] = []
    patterns = [
        r"(?:Учредител\w+|Участник\w+)\s*[:\-]\s*([^.;]{3,200})",
        r"(?:Состав\s+учредител\w+)\s*[:\-]\s*([^.;]{3,200})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = " ".join(match.group(1).split()).strip(" .,;:-")
            if value and value not in found:
                found.append(value)
    return found[:5]
