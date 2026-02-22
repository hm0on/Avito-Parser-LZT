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
import time
from pathlib import Path
from urllib.parse import quote_plus

import httpx
import structlog
from bs4 import BeautifulSoup

from src.collectors.base import AbstractCollector, RawCompany, RawReview, parse_float, parse_int
from src.config import settings
from src.proxy import proxy_manager

log = structlog.get_logger(__name__)

AVITO_BASE = "https://www.avito.ru"
AVITO_SEARCH = f"{AVITO_BASE}/omsk/predlozheniya_uslug"

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

    def fetch_phones(self, ad_ids: list[str]) -> dict[str, str]:
        """Fetch phone numbers for a batch of ad IDs via spfa.ru/api/phone/.

        Returns dict {ad_id: phone} for ads where phone was found.
        Processes in chunks of 50 (API limit).
        """
        import requests

        result: dict[str, str] = {}
        for i in range(0, len(ad_ids), 50):
            chunk = ad_ids[i : i + 50]
            try:
                r = requests.post(
                    f"{self._API}/phone/",
                    json={"api_key": self._api_key, "ads": chunk},
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                if data.get("success"):
                    for item in data.get("results", []):
                        phone = item.get("phone")
                        if phone:
                            result[str(item["ad_id"])] = phone
                    meta = data.get("meta", {})
                    log.info(
                        "avito.phones.batch_done",
                        chunk_size=len(chunk),
                        success=meta.get("success", 0),
                        time_sec=meta.get("time_sec"),
                    )
            except Exception as exc:
                log.warning("avito.phones.error", error=str(exc), chunk_start=i)
        return result

    def handle_block(self) -> None:
        """Request cookie unblock via spfa.ru/api/unblock/.

        Per docs: unblock is async (~5 sec), up to 12 req/min.
        After unblock we re-fetch cookies via /api/cookies/ to get refreshed set.
        """
        if not self._id:
            self._buy()
            return
        import requests

        # Throttle: don't spam unblock (max 12/min per docs)
        now = time.time()
        if self._unblock_started_at and now - self._unblock_started_at < 10:
            log.info("avito.cookies.waiting_unblock",
                     elapsed=round(now - self._unblock_started_at, 1))
            return

        try:
            r = requests.post(
                f"{self._API}/unblock/",
                json={"id": self._id, "api_key": self._api_key},
                timeout=15,
            )
            log.info("avito.cookies.unblock_response",
                     status=r.status_code, body=r.text[:200])

            if r.status_code in (200, 202):
                self._unblock_started_at = now
                # Unblock is async — wait for it to complete (~5 sec per docs)
                time.sleep(6)
                # Re-fetch updated cookies
                self._refresh_cookies()
                return

            if r.status_code == 409:
                # Already unblocking — wait and refresh
                self._unblock_started_at = self._unblock_started_at or now
                time.sleep(6)
                self._refresh_cookies()
                return

        except Exception as exc:
            log.warning("avito.cookies.unblock_error", error=str(exc))

        # Unblock failed — buy new cookies
        self._unblock_started_at = None
        self._buy()

    def _refresh_cookies(self) -> None:
        """Re-fetch cookie set by ID to get updated values after unblock."""
        import requests
        try:
            r = requests.post(
                f"{self._API}/cookies/",
                json={"api_key": self._api_key, "id": self._id},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json().get("results", {})
                new_cookies = data.get("cookies")
                if new_cookies:
                    self._cookies = new_cookies
                    self._save()
                    log.info("avito.cookies.refreshed", id=self._id,
                             count=len(new_cookies))
                    return
            log.debug("avito.cookies.refresh_no_update", status=r.status_code)
        except Exception as exc:
            log.warning("avito.cookies.refresh_error", error=str(exc))

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

    async def enrich_phones(self, companies: list[RawCompany]) -> None:
        """Phone enrichment via spfa.ru API — disabled to save costs.

        Phones are obtained from other sources (2GIS, Yandex, website scanner).
        """
        log.info("avito.phones.skipped", reason="disabled_to_save_costs")
        return

    async def enrich_reviews(self, companies: list[RawCompany]) -> None:
        """Fetch seller reviews for Avito companies using httpx (no Playwright).

        Strategy: ad page HTML → extract seller user path → fetch profile page
        → parse reviews from embedded JSON or DOM.
        """
        candidates = [
            c for c in companies
            if c.source == "avito" and (c.reviews_count or 0) > 0 and c.source_link
        ]
        if not candidates:
            return

        log.info("avito.reviews.start", count=len(candidates))

        for company in candidates:
            try:
                reviews = await self._fetch_seller_reviews(company)
                if reviews:
                    company.reviews = reviews
                    log.info(
                        "avito.reviews.ok",
                        name=company.name_raw,
                        count=len(reviews),
                    )
                else:
                    log.debug("avito.reviews.empty", name=company.name_raw)
            except Exception as exc:
                log.warning(
                    "avito.reviews.error",
                    name=company.name_raw,
                    error=str(exc),
                )
            await asyncio.sleep(2)

        total_reviews = sum(len(c.reviews) for c in candidates)
        log.info("avito.reviews.done", total_reviews=total_reviews)

    async def _fetch_seller_reviews(self, company: RawCompany) -> list[RawReview]:
        """Ad page → seller profile → ratings API → reviews.

        Uses self._fetch() for page loads (handles 429 + cookie renewal),
        then a fresh session with spfa cookies for the API call.
        """
        # Step 1: Fetch ad page → find seller profile link
        ad_html = await self._fetch(company.source_link)
        if not ad_html:
            log.debug("avito.reviews.ad_failed", url=company.source_link)
            return []

        seller_path = _extract_seller_path(ad_html)
        if not seller_path:
            log.debug("avito.reviews.no_seller", url=company.source_link)
            return []

        log.debug("avito.reviews.seller_found", seller=seller_path)

        # Step 2: Fetch seller profile page
        await asyncio.sleep(2)
        profile_url = f"{AVITO_BASE}{seller_path}"
        profile_html = await self._fetch(profile_url)
        if not profile_html:
            log.debug("avito.reviews.profile_failed")
            return []

        # Step 3: Extract ratings API URL from embedded JSON
        ratings_api = _extract_ratings_api_path(profile_html)
        if not ratings_api:
            log.debug("avito.reviews.no_api_url")
            # Fallback: try constructing the URL manually
            user_hash = _extract_user_hash(seller_path)
            if user_hash:
                ratings_api = f"/web/6/user/{user_hash}/ratings"
            else:
                return []

        log.debug("avito.reviews.api_url", api=ratings_api)

        # Step 4: Call ratings API with spfa cookies + XHR headers
        await asyncio.sleep(3)
        reviews = await self._fetch_ratings_api(ratings_api, profile_url, company.source_link)
        return reviews

    async def _fetch_ratings_api(
        self, api_path: str, referer: str, source_link: str,
    ) -> list[RawReview]:
        """Call Avito ratings API with retries, version fallback, and cookie renewal."""
        api_headers = {
            **_HEADERS,
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

        # Try multiple API versions (6, 5, 4) — Avito changes them periodically
        versions_to_try = [6, 5, 4]
        base_path = _re.sub(r"/web/\d+/", "/web/{ver}/", api_path)

        for ver in versions_to_try:
            path = base_path.replace("{ver}", str(ver))
            ratings_url = f"{AVITO_BASE}{path}"

            for attempt in range(1, 4):
                cookies = self._cookies_provider.get() if self._cookies_provider else {}
                proxy_url = proxy_manager.get_next()

                try:
                    async with httpx.AsyncClient(
                        proxy=proxy_url or None,
                        headers=api_headers,
                        cookies=cookies,
                        timeout=30.0,
                        follow_redirects=True,
                    ) as client:
                        resp = await client.get(ratings_url)

                        if resp.status_code == 200:
                            ct = resp.headers.get("content-type", "")
                            if "json" not in ct:
                                log.debug("avito.reviews.not_json", ct=ct, ver=ver)
                                break  # try next version
                            data = resp.json()
                            reviews = _parse_ratings_json(data, source_link)
                            log.info("avito.reviews.api_ok", count=len(reviews), ver=ver)
                            return reviews

                        if resp.status_code == 404:
                            log.debug("avito.reviews.api_404", ver=ver)
                            break  # try next version

                        if resp.status_code == 429:
                            wait = 5 * attempt
                            log.debug("avito.reviews.rate_limit", wait=wait, attempt=attempt)
                            if self._cookies_provider:
                                self._cookies_provider.handle_block()
                            await asyncio.sleep(wait)
                            continue

                        log.debug("avito.reviews.api_error", status=resp.status_code, ver=ver)
                        break  # try next version

                except Exception as exc:
                    log.warning("avito.reviews.api_exc", error=str(exc), attempt=attempt)
                    await asyncio.sleep(3 * attempt)

            await asyncio.sleep(1)

        return []

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

    async def _fetch(self, url: str) -> str | None:
        """Fetch with up to 3 retries; on 429 renews cookies and waits before retry."""
        for attempt in range(1, 4):
            proxy_url = proxy_manager.get_next()
            cookies = self._cookies_provider.get() if self._cookies_provider else {}

            try:
                async with httpx.AsyncClient(
                    proxy=proxy_url or None,
                    headers=_HEADERS,
                    cookies=cookies,
                    timeout=30.0,
                    follow_redirects=True,
                ) as client:
                    resp = await client.get(url)

                    if resp.status_code == 429:
                        wait = 5 * attempt
                        log.warning("avito.fetch.rate_limited",
                                    attempt=attempt, wait_s=wait, url=url)
                        if self._cookies_provider:
                            self._cookies_provider.handle_block()
                        await asyncio.sleep(wait)
                        continue

                    if resp.status_code == 403:
                        log.warning("avito.fetch.forbidden", url=url)
                        if self._cookies_provider:
                            self._cookies_provider.handle_block()
                        return None

                    resp.raise_for_status()
                    return resp.text

            except httpx.HTTPStatusError as exc:
                log.warning("avito.fetch.http_error", status=exc.response.status_code,
                            attempt=attempt, url=url)
                await asyncio.sleep(3 * attempt)
            except Exception as exc:
                log.warning("avito.fetch.error", error=str(exc), attempt=attempt, url=url)
                await asyncio.sleep(3 * attempt)

        log.warning("avito.fetch.gave_up", url=url)
        return None


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
    average_rating = parse_float(rating_el.get_text(strip=True)) if rating_el else None

    # Reviews count ("29 отзывов" → 29)
    reviews_el = card.find(attrs={"data-marker": "seller-info/summary"})
    reviews_count = parse_int(reviews_el.get_text(strip=True)) if reviews_el else None

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


# ─────────────────────────────────────────────────────────────────────────────
# Seller review extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

import html as _html_mod
import re as _re


def _extract_ratings_api_path(html: str) -> str | None:
    """Extract ratings API path from embedded JSON in the profile page.

    Avito embeds state as HTML-entity-encoded JSON in <script> tags.
    The API URL is in the 'nextPage' field under the 'rating' object.
    Returns the path with version normalized to 6 (latest working).
    """
    decoded = _html_mod.unescape(html)
    m = _re.search(r'"nextPage"\s*:\s*"(/web/\d+/user/[^"]+/ratings[^"]*)"', decoded)
    if not m:
        return None
    path = m.group(1)
    # Normalize API version to 6 (v4 often returns 404)
    path = _re.sub(r"/web/\d+/", "/web/6/", path)
    return path


def _extract_user_hash(seller_path: str) -> str | None:
    """Extract user hash from a seller profile path like /user/abc123/profile."""
    m = _re.search(r"/user/([a-zA-Z0-9_-]+)", seller_path)
    return m.group(1) if m else None


def _parse_ratings_json(data: dict, source_link: str) -> list[RawReview]:
    """Parse reviews from Avito's /web/N/user/{hash}/ratings JSON response.

    Avito API v6 returns:
      {"entries": [{"type": "rating", "value": {"title": "Author", "score": 5,
       "textSections": [{"text": "..."}], ...}}, ...]}
    """
    reviews: list[RawReview] = []

    # Format 1: entries-based (Avito API v6)
    for entry in data.get("entries", []):
        if not isinstance(entry, dict) or entry.get("type") != "rating":
            continue
        value = entry.get("value", {})
        if not isinstance(value, dict):
            continue

        # Text is in textSections[].text
        text_parts = []
        for section in value.get("textSections", []):
            if isinstance(section, dict) and section.get("text"):
                text_parts.append(section["text"])
        text = " ".join(text_parts).strip()

        if not text or len(text) < 10:
            continue

        score = value.get("score")
        author = value.get("title")  # author name in "title" field

        reviews.append(
            RawReview(
                source="avito",
                text=text[:500],
                rating=float(score) if score is not None else None,
                author=author,
                source_link=source_link,
            )
        )

    if reviews:
        return reviews

    # Format 2: flat list (older API versions or different structure)
    items = (
        data.get("reviews")
        or data.get("ratings")
        or data.get("items")
        or (data.get("result", {}).get("reviews") if isinstance(data.get("result"), dict) else None)
        or []
    )

    for item in items:
        if not isinstance(item, dict):
            continue

        text = (item.get("text") or item.get("body") or item.get("comment") or "").strip()
        if not text or len(text) < 10:
            continue

        score = item.get("score") or item.get("rating")
        author_data = item.get("sender") or item.get("author") or item.get("user")
        author = None
        if isinstance(author_data, dict):
            author = author_data.get("name") or author_data.get("public_name")
        elif isinstance(author_data, str):
            author = author_data

        reviews.append(
            RawReview(
                source="avito",
                text=text[:500],
                rating=float(score) if score is not None else None,
                author=author,
                source_link=source_link,
            )
        )

    return reviews


def _extract_seller_path(html: str) -> str | None:
    """Extract seller profile path from ad page HTML.

    Looks for links like /user/abc123def/ or /user/abc123def/profile.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Try data-marker based selectors first (most reliable)
    for el in soup.find_all("a", attrs={"data-marker": True}):
        marker = el.get("data-marker", "")
        if "seller" in marker.lower():
            href = el.get("href", "")
            if "/user/" in href:
                return _clean_seller_href(href)

    # Generic /user/ link search
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/user/" in href and "login" not in href and "registration" not in href:
            return _clean_seller_href(href)

    # Regex fallback: search raw HTML for user paths
    for m in _re.finditer(r'"/user/([a-zA-Z0-9_-]{5,})', html):
        slug = m.group(1)
        if slug not in ("login", "registration", "logout", "settings"):
            return f"/user/{slug}/profile"

    return None


def _clean_seller_href(href: str) -> str:
    """Normalize seller href to a profile path."""
    path = href.split("?")[0].rstrip("/")
    if not path.endswith("/profile"):
        path += "/profile"
    return path


def _parse_reviews_from_html(html: str, source_link: str) -> list[RawReview]:
    """Extract reviews from Avito profile/ratings page.

    Tries embedded JSON state first, then DOM fallback.
    """
    reviews: list[RawReview] = []

    # Method 1: Parse embedded JSON state (Avito React/SSR)
    for pattern in [
        r"window\.__initialState__\s*=\s*({.+?});\s*</",
        r"window\.__initial_state__\s*=\s*({.+?});\s*</",
        r"window\.__DATA__\s*=\s*({.+?});\s*</",
    ]:
        m = _re.search(pattern, html, _re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                reviews = _walk_json_for_reviews(data, source_link)
                if reviews:
                    return reviews
            except (json.JSONDecodeError, RecursionError):
                pass

    # Method 2: Parse DOM for review blocks
    soup = BeautifulSoup(html, "html.parser")

    # data-marker based blocks
    for block in soup.find_all(
        attrs={"data-marker": lambda v: v and ("review" in v.lower() or "rating" in v.lower())}
    ):
        review = _extract_single_review(block, source_link)
        if review:
            reviews.append(review)

    if reviews:
        return reviews

    # class-based blocks
    for block in soup.find_all(
        attrs={"class": lambda v: v and _has_review_class(v)}
    ):
        review = _extract_single_review(block, source_link)
        if review:
            reviews.append(review)

    return reviews


def _has_review_class(classes) -> bool:
    """Check if element has a review-related CSS class."""
    if isinstance(classes, list):
        return any("review" in c.lower() or "rating-item" in c.lower() for c in classes)
    return "review" in str(classes).lower()


def _extract_single_review(block, source_link: str) -> RawReview | None:
    """Extract a single review from a DOM block."""
    text = block.get_text(strip=True)
    if not text or len(text) < 20 or len(text) > 2000:
        return None
    # Skip navigation/system text
    skip_words = ["войти", "зарегистрировать", "avito", "продолжить", "подробнее", "показать ещё"]
    if any(s in text.lower() for s in skip_words):
        return None
    return RawReview(
        source="avito",
        text=text[:500],
        rating=None,
        author=None,
        source_link=source_link,
    )


def _walk_json_for_reviews(data, source_link: str, max_depth: int = 8) -> list[RawReview]:
    """Recursively search Avito's embedded JSON state for review objects."""
    reviews: list[RawReview] = []

    def _walk(obj, depth: int = 0):
        if depth > max_depth or len(reviews) >= 50:
            return
        if isinstance(obj, dict):
            # Check if this dict looks like a review item
            text = obj.get("text") or obj.get("body") or obj.get("comment")
            if text and isinstance(text, str) and len(text.strip()) >= 10:
                score = obj.get("score") or obj.get("rating")
                author_data = obj.get("sender") or obj.get("author") or obj.get("user")
                author = None
                if isinstance(author_data, dict):
                    author = author_data.get("name") or author_data.get("public_name")
                elif isinstance(author_data, str):
                    author = author_data
                reviews.append(
                    RawReview(
                        source="avito",
                        text=text.strip()[:500],
                        rating=float(score) if score is not None else None,
                        author=author,
                        source_link=source_link,
                    )
                )
                return  # Don't recurse deeper inside a review object
            for val in obj.values():
                if isinstance(val, (dict, list)):
                    _walk(val, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    _walk(item, depth + 1)

    _walk(data)
    return reviews
