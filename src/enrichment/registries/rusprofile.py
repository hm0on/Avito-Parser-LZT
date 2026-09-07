"""Rusprofile registry checker — secondary fallback after list-org."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin

import httpx
import structlog
from bs4 import BeautifulSoup

from src.config import settings
from src.enrichment.registries.base import AbstractChecker, CheckResult
from src.proxy import enrichment_proxy_manager, iter_proxy_urls

log = structlog.get_logger(__name__)

_RUSPROFILE_BASE = "https://www.rusprofile.ru"
_RUSPROFILE_SEARCH_URL = f"{_RUSPROFILE_BASE}/search"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*",
}


class RusprofileChecker(AbstractChecker):
    registry_name = "rusprofile"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
    ) -> CheckResult:
        if not settings.rusprofile_enabled:
            return CheckResult(registry=self.registry_name, found=False, error="rusprofile disabled")

        queries = _build_queries(inn=inn, ogrn=ogrn, name=name)
        if not queries:
            return CheckResult(registry=self.registry_name, found=False, error="no query provided")

        best_candidate: dict | None = None
        for query in queries:
            html = await self._fetch_search_html(query)
            if not html:
                continue
            if _is_zero_results(html):
                log.info("rusprofile.search.not_found", query=query)
                continue

            candidates = _extract_search_candidates(html)
            selected = _select_best_candidate(candidates, inn=inn, ogrn=ogrn, name=name)
            if selected:
                selected["query"] = query
                best_candidate = selected
                break

        if not best_candidate:
            return CheckResult(registry=self.registry_name, found=False)

        card_html = await self._fetch_company_html(best_candidate["url"])
        if not card_html:
            return CheckResult(registry=self.registry_name, found=False, error="company card fetch failed")

        parsed = _parse_company_page(card_html, fallback_name=best_candidate.get("name"))
        details = {
            "name": parsed.get("name"),
            "inn": parsed.get("inn"),
            "ogrn": parsed.get("ogrn"),
            "status": parsed.get("status"),
            "entity_type": parsed.get("entity_type"),
            "match_method": best_candidate.get("match_method"),
            "director_name": parsed.get("director_name"),
            "director_position": parsed.get("director_position"),
            "founders": parsed.get("founders") or [],
            "source_url": best_candidate["url"],
            "match_score": best_candidate.get("score", 0.0),
            "query": best_candidate.get("query"),
        }

        found = bool(details.get("name") or details.get("inn") or details.get("ogrn"))
        log.info(
            "rusprofile.parse.done",
            query=best_candidate.get("query"),
            name=details.get("name"),
            inn=details.get("inn"),
            ogrn=details.get("ogrn"),
            status=details.get("status"),
        )
        return CheckResult(
            registry=self.registry_name,
            found=found,
            status=details.get("status"),
            details=details,
        )

    async def _fetch_search_html(self, query: str) -> str | None:
        return await self._fetch(
            _RUSPROFILE_SEARCH_URL,
            params={"query": query},
            log_key="rusprofile.search.fetch",
            query=query,
        )

    async def _fetch_company_html(self, company_url: str) -> str | None:
        return await self._fetch(
            company_url,
            params=None,
            log_key="rusprofile.card.fetch",
            query=company_url,
        )

    async def _fetch(
        self,
        url: str,
        *,
        params: dict | None,
        log_key: str,
        query: str,
    ) -> str | None:
        timeout = max(5, settings.rusprofile_timeout_seconds)
        max_attempts = max(1, settings.rusprofile_max_attempts)
        rps_delay = 1.0 / max(0.2, settings.listorg_rate_limit_rps)

        if settings.rusprofile_use_enrichment_proxy and enrichment_proxy_manager.count > 0:
            proxy_candidates = list(
                iter_proxy_urls(
                    enrichment_proxy_manager,
                    purpose="rusprofile registry",
                    max_attempts=max_attempts,
                )
            )
            # If proxy-auth/network blocks all attempts, try direct once.
            proxy_candidates.append(None)
        else:
            proxy_candidates = [None] * max_attempts

        last_error = ""
        for proxy_url in proxy_candidates:
            try:
                async with httpx.AsyncClient(
                    timeout=float(timeout),
                    headers=_HEADERS,
                    proxy=proxy_url,
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(url, params=params)
                if resp.status_code in (403, 429, 503, 407):
                    last_error = f"HTTP {resp.status_code}"
                    log.warning(log_key + ".blocked", status=resp.status_code, proxy=proxy_url or "direct", query=query)
                    await asyncio.sleep(rps_delay)
                    continue
                resp.raise_for_status()
                return resp.text
            except Exception as exc:
                last_error = str(exc)
                log.warning(log_key + ".error", error=last_error[:220], proxy=proxy_url or "direct", query=query)
                await asyncio.sleep(rps_delay)

        log.error(log_key + ".exhausted", query=query, last_error=last_error[:220])
        return None


def map_rusprofile_to_registries(rusprofile_result: CheckResult) -> dict[str, dict]:
    if not rusprofile_result.found:
        return {}

    details = rusprofile_result.details or {}
    status_raw = str(details.get("status") or "").lower()
    mapped_status = "ACTIVE"
    if any(marker in status_raw for marker in ("ликвид", "прекращ", "исключ")):
        mapped_status = "liquidated"
    elif "банкрот" in status_raw:
        mapped_status = "bankrupt"
    elif "active" in status_raw or "действ" in status_raw:
        mapped_status = "ACTIVE"

    return {
        "dadata_fns": {
            "registry": "dadata_fns",
            "found": True,
            "status": mapped_status,
            "details": {
                "name": details.get("name"),
                "inn": details.get("inn"),
                "ogrn": details.get("ogrn"),
                "entity_type": details.get("entity_type") or "unknown",
                "source": "rusprofile",
                "source_url": details.get("source_url"),
                "match_score": details.get("match_score"),
                "match_method": details.get("match_method") or "name_or_id",
                "director_name": details.get("director_name"),
                "director_position": details.get("director_position"),
                "founders": details.get("founders") or [],
            },
            "error": None,
        }
    }


def _build_queries(*, inn: str | None, ogrn: str | None, name: str | None) -> list[str]:
    queries: list[str] = []
    for value in (inn, ogrn):
        if value and str(value).strip():
            queries.append(str(value).strip())
    if settings.rusprofile_allow_name_lookup and name and name.strip():
        name_query = name.strip()
        region = settings.region.strip()
        if region and region.lower() not in name_query.lower():
            name_query = f"{name_query} {region}"
        queries.append(name_query)

    out: list[str] = []
    seen: set[str] = set()
    for query in queries:
        if query not in seen:
            seen.add(query)
            out.append(query)
    return out


def _is_zero_results(html: str) -> bool:
    return bool(re.search(r"было найдено\s+0\s+результат", html, flags=re.IGNORECASE))


def _extract_search_candidates(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for anchor in soup.select("a.list-element__title[href]"):
        href = (anchor.get("href") or "").strip()
        if not href.startswith("/id/"):
            continue
        name = anchor.get_text(" ", strip=True)
        container = anchor.find_parent(["div", "li", "article"])
        block_text = container.get_text(" ", strip=True) if container else anchor.get_text(" ", strip=True)
        inn = _extract_inn(block_text)
        ogrn = _extract_ogrn(block_text)
        out.append(
            {
                "name": name,
                "url": urljoin(_RUSPROFILE_BASE, href),
                "inn": inn,
                "ogrn": ogrn,
            }
        )
        if len(out) >= 20:
            break
    return out


def _select_best_candidate(
    candidates: list[dict],
    *,
    inn: str | None,
    ogrn: str | None,
    name: str | None,
) -> dict | None:
    if not candidates:
        return None

    if inn:
        for cand in candidates:
            if cand.get("inn") == inn:
                cand["score"] = 1.0
                cand["match_method"] = "inn"
                return cand

    if ogrn:
        for cand in candidates:
            if cand.get("ogrn") == ogrn:
                cand["score"] = 1.0
                cand["match_method"] = "ogrn"
                return cand

    query_tokens = _tokenize(name or "")
    if not query_tokens:
        return None

    best: dict | None = None
    best_score = -1.0
    for cand in candidates:
        score = _name_match_score(query_tokens, _tokenize(cand.get("name", "")))
        cand["score"] = score
        if score > best_score:
            best_score = score
            best = cand
    threshold = max(0.0, min(1.0, settings.rusprofile_name_match_min_score))
    if best is not None and best_score < threshold:
        return None
    if best is not None:
        best["match_method"] = "name"
    return best


def _parse_company_page(html: str, *, fallback_name: str | None) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    title = (soup.title.string.strip() if soup.title and soup.title.string else "") or ""

    name = _extract_company_name_from_title(title) or fallback_name
    inn = _extract_inn(text) or _extract_inn(title)
    ogrn = _extract_ogrn(text) or _extract_ogrn(title)
    status = _extract_legal_status(text)
    entity_type = _detect_entity_type(name or "", text)
    director_name, director_position = _extract_director_info(text)
    founders = _extract_founders(text)

    return {
        "name": name,
        "inn": inn,
        "ogrn": ogrn,
        "status": status,
        "entity_type": entity_type,
        "director_name": director_name,
        "director_position": director_position,
        "founders": founders,
    }


def _extract_company_name_from_title(title: str) -> str | None:
    if not title:
        return None
    # Example: ООО "Кех Екоммерц" Москва (ИНН 7710668349) адрес...
    cut = title.split(" (ИНН")[0].strip()
    return cut or None


def _extract_inn(text: str) -> str | None:
    match = re.search(r"ИНН\D{0,8}(\d{10}|\d{12})", text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _extract_ogrn(text: str) -> str | None:
    match = re.search(r"ОГРН(?:ИП)?\D{0,8}(\d{13}|\d{15})", text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _extract_legal_status(text: str) -> str:
    lower = text.lower()
    if "действующ" in lower or "active" in lower:
        return "active"
    if any(marker in lower for marker in ("ликвидир", "ликвидирован", "прекращен", "исключен")):
        return "liquidated"
    if "банкрот" in lower:
        return "bankrupt"
    return "unknown"


def _detect_entity_type(name: str, text: str) -> str:
    combined = f"{name} {text}".upper()
    if " ИП " in f" {combined} " or "ОГРНИП" in combined:
        return "ИП"
    if any(marker in combined for marker in ("ООО", "АО", "ПАО", "ЗАО", "ОАО", "МКООО")):
        return "ЮЛ"
    return "unknown"


def _tokenize(value: str) -> set[str]:
    tokens = set(re.findall(r"[a-zа-я0-9]+", (value or "").lower(), flags=re.IGNORECASE))
    return {
        token
        for token in tokens
        if token not in {"ооо", "ип", "ао", "пао", "зао", "оао", "мкооо", "компания", "омск"}
    }


def _name_match_score(lhs: set[str], rhs: set[str]) -> float:
    if not lhs or not rhs:
        return 0.0
    overlap = len(lhs & rhs)
    return overlap / max(1, len(lhs))


def _extract_director_info(text: str) -> tuple[str | None, str | None]:
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


def _extract_founders(text: str) -> list[str]:
    out: list[str] = []
    patterns = [
        r"(?:Учредител\w+|Участник\w+)\s*[:\-]\s*([^.;]{3,200})",
        r"(?:Состав\s+учредител\w+)\s*[:\-]\s*([^.;]{3,200})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = " ".join(match.group(1).split()).strip(" .,;:-")
            if value and value not in out:
                out.append(value)
    return out[:5]
