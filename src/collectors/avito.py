"""Avito collector — httpx + BeautifulSoup HTML parsing.

Anti-block strategy:
  1. Residential proxy (PROXY_URL / PROXY_FILE)
  2. spfa.ru cookies (AVITO_COOKIES_API_KEY) — valid Avito session, ~12₽/set, 12h
  3. Auto-renewal: on 403 the provider requests unblock or buys fresh cookies

Data source: Avito renders listings as HTML with data-marker attributes.
Parser: BeautifulSoup with html.parser (lxml strips data-* attributes).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import structlog
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from src.collectors.base import AbstractCollector, RawCompany
from src.config import settings
from src.proxy import proxy_manager

log = structlog.get_logger(__name__)

AVITO_BASE = "https://www.avito.ru"
AVITO_SEARCH = f"{AVITO_BASE}/omsk/uslugi"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Referer": "https://www.google.ru/search?q=avito+%D0%BE%D0%BC%D1%81%D0%BA",
}

_BLOCK_MARKERS = ["доступ ограничен", "firewall-container", "captcha", "access denied"]


# ─────────────────────────────────────────────────────────────────────────────
# Cookie provider (spfa.ru)
# ─────────────────────────────────────────────────────────────────────────────

class _SpfaCookiesProvider:
    """Provides valid Avito session cookies via spfa.ru API.

    Pricing: ~12₽ per cookie set, lasts 12+ hours.
    Register at https://spfa.ru and get an API key.
    """

    _API = "https://spfa.ru/api"
    _STORAGE = Path("storage/avito_cookies.json")

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._id: str | None = None
        self._cookies: dict | None = None
        self._unblock_started_at: float | None = None
        self._UNBLOCK_TIMEOUT = 300
        self._load()

    def get(self) -> dict:
        return self._cookies or {}

    def handle_block(self) -> None:
        if not self._id:
            self._buy()
            return
        import requests
        now = time.time()
        if self._unblock_started_at:
            if now - self._unblock_started_at < self._UNBLOCK_TIMEOUT:
                log.info("avito.cookies.waiting_unblock")
                return
            self._unblock_started_at = None
        try:
            r = requests.post(
                f"{self._API}/unblock/",
                json={"id": self._id, "api_key": self._api_key},
                timeout=15,
            )
            if r.status_code in (200, 202):
                self._unblock_started_at = now
                return
            if r.status_code == 409:
                self._unblock_started_at = self._unblock_started_at or now
                return
        except Exception as exc:
            log.warning("avito.cookies.unblock_error", error=str(exc))
        self._unblock_started_at = None
        self._buy()

    def _buy(self) -> None:
        import requests
        try:
            r = requests.post(
                f"{self._API}/cookies/",
                json={"api_key": self._api_key},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json().get("results", {})
            self._id = data.get("id")
            self._cookies = data.get("cookies")
            self._save()
            log.info("avito.cookies.bought", id=self._id)
        except Exception as exc:
            log.warning("avito.cookies.buy_error", error=str(exc))

    def _load(self) -> None:
        if not self._STORAGE.exists():
            return
        try:
            d = json.loads(self._STORAGE.read_text(encoding="utf-8"))
            self._id = d.get("id")
            self._cookies = d.get("cookies")
            if self._id:
                log.info("avito.cookies.loaded", id=self._id)
        except Exception:
            pass

    def _save(self) -> None:
        self._STORAGE.parent.mkdir(parents=True, exist_ok=True)
        self._STORAGE.write_text(
            json.dumps({"id": self._id, "cookies": self._cookies, "saved_at": time.time()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _build_cookies_provider() -> _SpfaCookiesProvider | None:
    api_key = getattr(settings, "avito_cookies_api_key", "")
    if api_key:
        log.info("avito.cookies.spfa_enabled")
        return _SpfaCookiesProvider(api_key)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Collector
# ─────────────────────────────────────────────────────────────────────────────

class AvitoCollector(AbstractCollector):
    source_name = "avito"

    def __init__(self) -> None:
        self._cookies_provider = _build_cookies_provider()

    async def collect(self, keyword: str) -> list[RawCompany]:
        url = f"{AVITO_SEARCH}?q={quote_plus(keyword)}"
        log.info("avito.collect.start", keyword=keyword, url=url)

        companies: list[RawCompany] = []

        for page in range(1, 6):  # max 5 pages × 50 items = 250
            page_url = f"{url}&p={page}" if page > 1 else url
            html_text = await self._fetch(page_url)

            if not html_text:
                break

            if _is_blocked(html_text):
                log.warning("avito.collect.blocked", page=page, keyword=keyword)
                if self._cookies_provider:
                    self._cookies_provider.handle_block()
                break

            page_companies = _parse_html(html_text, keyword)
            if not page_companies:
                log.info("avito.collect.empty_page", page=page, keyword=keyword)
                break

            companies.extend(page_companies)
            log.info(
                "avito.collect.page_done",
                page=page, found=len(page_companies), total=len(companies), keyword=keyword,
            )
            await asyncio.sleep(1.5)

        log.info("avito.collect.done", keyword=keyword, count=len(companies))
        return companies

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=15))
    async def _fetch(self, url: str) -> str | None:
        proxy_url = proxy_manager.get_next()
        cookies = self._cookies_provider.get() if self._cookies_provider else {}

        try:
            async with httpx.AsyncClient(
                proxy=proxy_url or None,
                headers=_HEADERS,
                timeout=30.0,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url, cookies=cookies)

                if resp.status_code in (403, 429):
                    log.warning("avito.fetch.blocked", status=resp.status_code, url=url)
                    if self._cookies_provider:
                        self._cookies_provider.handle_block()
                    return None

                resp.raise_for_status()
                return resp.text

        except Exception as exc:
            log.warning("avito.fetch.error", url=url, error=str(exc))
            raise  # tenacity retries


# ─────────────────────────────────────────────────────────────────────────────
# HTML parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_html(html_text: str, keyword: str) -> list[RawCompany]:
    """Parse listing cards from Avito HTML.

    Uses html.parser — lxml incorrectly strips data-* attributes on this page.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    cards = soup.find_all(attrs={"data-item-id": True})

    companies: list[RawCompany] = []
    for card in cards:
        company = _parse_card(card, keyword)
        if company:
            companies.append(company)
    return companies


def _parse_card(card, keyword: str) -> RawCompany | None:
    item_id = card.get("data-item-id")

    # Title + link
    title_el = card.find(attrs={"data-marker": "item-title"})
    name_raw = title_el.get_text(strip=True) if title_el else None
    if not name_raw:
        return None

    href = title_el.get("href", "") if title_el else ""
    # Strip ?context=... tracking parameter from the URL
    clean_path = href.split("?")[0]
    source_link = f"{AVITO_BASE}{clean_path}" if clean_path.startswith("/") else None

    # Location / address
    loc_el = card.find(attrs={"data-marker": "item-location"})
    addresses: list[str] = []
    if loc_el:
        loc_text = loc_el.get_text(strip=True)
        if loc_text:
            addresses.append(loc_text)

    # Rating (try specific first, then generic)
    rating_el = card.find(attrs={"data-marker": "seller-rating/score"}) or \
                card.find(attrs={"data-marker": "seller-rating"})
    average_rating = _parse_float(rating_el.get_text(strip=True)) if rating_el else None

    # Reviews count ("29 отзывов" → 29)
    reviews_el = card.find(attrs={"data-marker": "seller-info/summary"})
    reviews_count = _parse_int(reviews_el.get_text(strip=True)) if reviews_el else None

    return RawCompany(
        source="avito",
        source_id=item_id,
        source_link=source_link,
        name_raw=name_raw,
        addresses=addresses,
        average_rating=average_rating,
        reviews_count=reviews_count,
        raw_payload={"keyword": keyword, "item_id": item_id},
    )


def _is_blocked(html_text: str) -> bool:
    lower = html_text[:3000].lower()
    return any(m in lower for m in _BLOCK_MARKERS)


def _parse_float(text: str) -> float | None:
    m = re.search(r"[\d]+[,.]?[\d]*", text.replace(",", "."))
    try:
        return float(m.group().replace(",", ".")) if m else None
    except ValueError:
        return None


def _parse_int(text: str) -> int | None:
    m = re.search(r"\d+", text)
    try:
        return int(m.group()) if m else None
    except ValueError:
        return None
