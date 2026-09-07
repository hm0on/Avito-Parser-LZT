"""Yandex Maps collector — Playwright scraper with state-view JSON parsing."""

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse

import structlog
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.collectors.base import AbstractCollector, RawCompany, RawReview, parse_float, parse_int
from src.config import settings
from src.proxy import proxy_log_hint, require_proxy_pool, yandex_proxy_manager

log = structlog.get_logger(__name__)

YANDEX_MAPS_ROOT = "https://yandex.ru/maps/"


class BotChallengeError(RuntimeError):
    """Raised when Yandex anti-bot challenge page is detected."""


class YandexCollector(AbstractCollector):
    source_name = "yandex"
    _SCROLL_IDLE_SECONDS = 30.0
    _SCROLL_STEP_PAUSE_SECONDS = 0.8
    _SCROLL_MAX_SECONDS = 240.0
    _REVIEWS_TAB_MAX_ATTEMPTS = 2
    _REVIEWS_SCROLL_STEP_PAUSE_SECONDS = 0.8
    _REVIEWS_SCROLL_MAX_SECONDS = 240.0
    _REVIEWS_SCROLL_STABLE_STEPS = 4
    _max_concurrent_keywords = 0  # from config
    _uses_playwright = True

    async def collect(self, keyword: str, *, max_companies: int = 0) -> list[RawCompany]:
        urls = self._build_search_urls(keyword)
        log.info("yandex.collect.start", keyword=keyword, urls=urls)

        companies = await self._collect_urls(urls, keyword, max_companies=max_companies)
        log.info("yandex.collect.done", keyword=keyword, count=len(companies))
        return companies

    async def collect_from_url(self, url: str, *, keyword: str = "manual_url") -> list[RawCompany]:
        resolved_keyword = self._resolve_keyword_for_url(keyword, url)
        if resolved_keyword != keyword:
            log.info("yandex.collect_url.keyword_fallback", original=keyword, resolved=resolved_keyword)
        log.info("yandex.collect_url.start", keyword=resolved_keyword, url=url)
        companies = await self._collect_urls([url], resolved_keyword)
        log.info("yandex.collect_url.done", keyword=resolved_keyword, url=url, count=len(companies))
        return companies

    def _resolve_keyword_for_url(self, keyword: str, url: str) -> str:
        from_url = self._extract_keyword_from_url(url)
        if not from_url:
            return keyword
        raw = (keyword or "").strip()
        if not raw or raw == "manual_url":
            return from_url
        if self._looks_broken_keyword(raw):
            return from_url
        return keyword

    def _extract_keyword_from_url(self, url: str) -> str | None:
        try:
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
        except Exception:
            return None
        text_values = query.get("text") or []
        if not text_values:
            return None
        value = text_values[0]
        decoded = unquote_plus(value).strip()
        return decoded or None

    def _looks_broken_keyword(self, keyword: str) -> bool:
        if not keyword:
            return True
        if "�" in keyword:
            return True
        letters = [ch for ch in keyword if ch.isalpha()]
        if letters and all(ord(ch) < 128 for ch in letters):
            return False
        non_space = [ch for ch in keyword if not ch.isspace()]
        if not non_space:
            return True
        question_marks = sum(1 for ch in non_space if ch == "?")
        return (question_marks / len(non_space)) >= 0.3

    async def _collect_urls(
        self,
        urls: list[str],
        keyword: str,
        *,
        max_companies: int = 0,
    ) -> list[RawCompany]:
        require_proxy_pool(yandex_proxy_manager, purpose="yandex collection")
        if settings.yandex_single_pass:
            if not urls:
                return []
            if yandex_proxy_manager.count <= 0:
                rotate_sx_ru = getattr(yandex_proxy_manager, "rotate_sx_ru", None)
                if callable(rotate_sx_ru):
                    rotated = rotate_sx_ru()
                    if asyncio.iscoroutine(rotated):
                        rotated = await rotated
                    if rotated:
                        log.info("yandex.collect.proxy_rotated", proxy=proxy_log_hint(rotated))
            if yandex_proxy_manager.count <= 0:
                raise RuntimeError(
                    "Proxy is required for Yandex but no proxies are configured "
                    f"(YANDEX_PROXY_FILE={bool(settings.yandex_proxy_file)}, "
                    f"YANDEX_PROXY_URL={bool(settings.yandex_proxy_url)}, "
                    f"SX_PROXY_API_KEY={bool(settings.sx_proxy_api_key)})"
                )
            return await self._collect_once(
                urls[0],
                keyword,
                use_proxy=True,
                max_companies=max_companies,
            )

        companies: list[RawCompany] = []
        for url in urls:
            try:
                companies = await self._collect_once(
                    url,
                    keyword,
                    use_proxy=True,
                    max_companies=max_companies,
                )
                if companies:
                    break
            except Exception as exc:
                log.warning("yandex.collect.with_proxy_error", url=url, error=_exc_detail(exc))

        return companies

    async def _collect_once(
        self,
        url: str,
        keyword: str,
        *,
        use_proxy: bool,
        max_companies: int = 0,
    ) -> list[RawCompany]:
        if not use_proxy:
            raise RuntimeError("Direct Yandex mode is disabled; proxy is required")
        attempts = max(1, int(settings.yandex_browser_restart_attempts))
        last_exc: Exception | None = None
        async with async_playwright() as pw:
            for attempt in range(1, attempts + 1):
                browser = await self._launch_browser(pw, use_proxy=use_proxy)
                context: BrowserContext | None = None
                page: Page | None = None
                try:
                    context = await self._new_context(browser)
                    page = await context.new_page()
                    await self._attach_debug_hooks(page)
                    return await self._scrape_listing(
                        page,
                        url,
                        keyword,
                        max_companies=max_companies,
                    )
                except BotChallengeError as exc:
                    last_exc = exc
                    log.warning(
                        "yandex.collect.bot_challenge",
                        attempt=attempt,
                        attempts=attempts,
                        url=url,
                        use_proxy=use_proxy,
                    )
                    if attempt >= attempts:
                        raise
                except Exception as exc:
                    last_exc = exc
                    log.warning(
                        "yandex.collect.attempt_error",
                        attempt=attempt,
                        attempts=attempts,
                        url=url,
                        use_proxy=use_proxy,
                        error=str(exc)[:200],
                    )
                    if attempt >= attempts:
                        raise
                finally:
                    await self._debug_hold("before_close")
                    if page:
                        await page.close()
                    if context:
                        await context.close()
                    await browser.close()
        if last_exc:
            raise last_exc
        return []

    async def _launch_browser(self, pw, *, use_proxy: bool) -> Browser:
        if not use_proxy:
            raise RuntimeError("Direct Yandex browser launch is disabled")
        require_proxy_pool(yandex_proxy_manager, purpose="yandex browser launch")
        proxy = yandex_proxy_manager.playwright_proxy()
        if not proxy:
            raise RuntimeError("Failed to acquire Yandex proxy from configured pool")
        debug = settings.yandex_debug_browser
        slow_mo = max(0, int(settings.yandex_debug_slow_mo_ms)) if debug else 0
        log.info(
            "yandex.debug.launch",
            debug=debug,
            slow_mo=slow_mo,
            hold_s=settings.yandex_debug_hold_seconds,
            use_proxy=use_proxy,
            proxy=proxy.get("server") if proxy else "missing",
        )
        return await pw.chromium.launch(
            headless=not debug,
            proxy=proxy,
            slow_mo=slow_mo,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

    async def _new_context(self, browser: Browser) -> BrowserContext:
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="ru-RU",
        )
        try:
            from playwright_stealth import stealth_async

            await stealth_async(context)
        except ImportError:
            pass
        return context

    async def _scrape_listing(
        self,
        page: Page,
        url: str,
        keyword: str,
        *,
        max_companies: int = 0,
    ) -> list[RawCompany]:
        try:
            resp = await self._goto_with_fallback(
                page,
                url,
                timeout_ms=45_000,
                purpose="listing",
            )
        except Exception:
            await self._debug_screenshot(page, "goto_error")
            raise
        status = resp.status if resp else None
        if status and status >= 400:
            await self._debug_screenshot(page, "goto_http_error")
            raise RuntimeError(f"Yandex goto HTTP {status} for {url}")
        if await self._is_bot_challenge_page(page):
            await self._debug_screenshot(page, "bot_challenge")
            raise BotChallengeError("Yandex anti-bot challenge detected")
        await self._debug_screenshot(page, "after_goto")
        await self._debug_hold("after_goto")
        await asyncio.sleep(2)
        await self._wait_results_ready(page)

        # DOM-based flow: scroll list until it stops growing, then parse visible cards.
        cards = await self._collect_cards_with_scroll(page, max_cards=max_companies)
        if max_companies:
            cards = cards[:max_companies]
        log.info("yandex.listing.cards_collected", count=len(cards), keyword=keyword)

        companies: list[RawCompany] = []
        seen_ids: set[str] = set()
        for card in cards:
            try:
                company = await self._parse_card(page, card, keyword)
                if company:
                    # Deduplicate by source_id (from Denis)
                    if company.source_id and company.source_id in seen_ids:
                        continue
                    if company.source_id:
                        seen_ids.add(company.source_id)
                    companies.append(company)
                    if max_companies and len(companies) >= max_companies:
                        break
            except Exception as exc:
                log.warning("yandex.card.parse_error", error=str(exc))

        # After scroll-based loading, parse state-view JSON and merge with DOM cards.
        companies_from_state = await self._parse_state_view_companies(page, keyword)
        if companies_from_state:
            log.info(
                "yandex.listing.parsed_from_state_view_after_scroll",
                count=len(companies_from_state),
                dom_count=len(companies),
            )
            companies = self._merge_state_into_dom_companies(companies, companies_from_state)
            if max_companies:
                companies = companies[:max_companies]

        await self._collect_reviews_for_listing(page, companies)

        await self._debug_screenshot(page, "after_parse")
        await self._debug_hold("after_parse")
        return companies

    async def _goto_with_fallback(
        self,
        page: Page,
        url: str,
        *,
        timeout_ms: int,
        purpose: str,
    ):
        try:
            return await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            # Yandex Maps often keeps background requests alive, so stricter load states
            # are unreliable through proxies. Fall back to the earliest committed response.
            if "timeout" not in str(exc).lower():
                raise
            fallback_timeout_ms = min(15_000, max(5_000, timeout_ms // 3))
            log.warning(
                "yandex.goto.timeout_fallback",
                purpose=purpose,
                url=url,
                timeout_ms=timeout_ms,
                fallback_timeout_ms=fallback_timeout_ms,
                error=str(exc)[:200],
            )
            return await page.goto(url, wait_until="commit", timeout=fallback_timeout_ms)

    async def _parse_state_view_companies(self, page: Page, keyword: str) -> list[RawCompany]:
        script = await page.query_selector("script.state-view[type='application/json']")
        if not script:
            return []

        try:
            raw = (await script.inner_text()).strip()
            state = json.loads(raw)
        except Exception as exc:
            log.warning("yandex.state_view.parse_error", error=str(exc))
            return []

        stack = state.get("stack") or []
        if not stack or not isinstance(stack[0], dict):
            return []
        results = stack[0].get("results") or {}
        items = results.get("items") or []
        if not isinstance(items, list):
            return []

        companies: list[RawCompany] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue

            source_id = str(item.get("id") or "").strip() or None
            name_raw = (item.get("title") or item.get("shortTitle") or "").strip() or None
            if not source_id and not name_raw:
                continue

            dedup_key = source_id or f"name:{name_raw}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            address = (
                item.get("fullAddress")
                or item.get("address")
                or ""
            ).strip()
            addresses = [address] if address else []

            rating_data = item.get("ratingData") or {}
            average_rating = rating_data.get("ratingValue")
            reviews_count = rating_data.get("reviewCount")
            if reviews_count in (None, 0):
                reviews_count = rating_data.get("ratingCount")

            phones: list[str] = []
            for p in item.get("phones") or []:
                if isinstance(p, dict):
                    value = (p.get("number") or "").strip()
                    if value:
                        phones.append(value)

            social_links: list[str] = []
            for s in item.get("socialLinks") or []:
                if isinstance(s, dict):
                    href = (s.get("href") or "").strip()
                    if href:
                        social_links.append(href)

            sites: list[str] = []
            for u in item.get("urls") or []:
                if isinstance(u, str) and u.strip():
                    sites.append(u.strip())

            categories: list[str] = []
            for c in item.get("categories") or []:
                if isinstance(c, dict):
                    cat_name = (c.get("name") or "").strip()
                    if cat_name:
                        categories.append(cat_name)

            features_map: dict[str, object] = {}
            for feat in item.get("features") or []:
                if isinstance(feat, dict):
                    fid = (feat.get("id") or "").strip()
                    if fid:
                        features_map[fid] = feat.get("value")

            business_links: list[str] = []
            for b in item.get("businessLinks") or []:
                if isinstance(b, dict):
                    href = (b.get("href") or "").strip()
                    if href:
                        business_links.append(href)

            source_link = self._state_view_source_link(item, source_id)
            coordinates = item.get("coordinates") or []
            lon = coordinates[0] if isinstance(coordinates, list) and len(coordinates) > 1 else None
            lat = coordinates[1] if isinstance(coordinates, list) and len(coordinates) > 1 else None

            company = RawCompany(
                source=self.source_name,
                source_id=source_id,
                source_link=source_link,
                name_raw=name_raw,
                phones=list(dict.fromkeys(phones)),
                addresses=addresses,
                average_rating=average_rating,
                reviews_count=reviews_count,
                contacts_json={
                    "websites": list(dict.fromkeys(sites)),
                    "social_links": list(dict.fromkeys(social_links)),
                    "categories": list(dict.fromkeys(categories)),
                    "business_links": list(dict.fromkeys(business_links)),
                    "features": features_map,
                    "working_time_text": item.get("workingTimeText"),
                    "working_time": item.get("workingTime"),
                    "current_working_status": (item.get("currentWorkingStatus") or {}).get("text"),
                    "composite_address": item.get("compositeAddress"),
                    "subtitle_items": item.get("subtitleItems"),
                    "urls": item.get("urls"),
                    "coordinates": {"lon": lon, "lat": lat},
                },
                raw_payload={
                    "keyword": keyword,
                    "state_item": item,
                },
            )
            companies.append(company)

        return companies

    def _state_view_source_link(self, item: dict, source_id: str | None) -> str | None:
        uri = (item.get("uri") or "").strip()
        if uri.startswith("http://") or uri.startswith("https://"):
            return uri
        if source_id:
            seoname = (item.get("seoname") or "").strip()
            if seoname:
                return f"https://yandex.ru/maps/org/{seoname}/{source_id}/"
            return f"https://yandex.ru/maps/org/{source_id}/"
        return None

    async def _collect_cards_with_scroll(self, page: Page, *, max_cards: int = 0) -> list:
        start = time.monotonic()
        last_new = start
        best_count = 0
        zero_placeholders_streak = 0

        while True:
            cards_count, placeholders_count = await self._results_stats(page)
            if cards_count > best_count:
                best_count = cards_count
                last_new = time.monotonic()
                log.info(
                    "yandex.scroll.new_cards",
                    cards=best_count,
                    placeholders=placeholders_count,
                )
            if max_cards and best_count >= max_cards:
                log.info(
                    "yandex.scroll.stop_limit_reached",
                    cards=best_count,
                    limit=max_cards,
                )
                break

            # All placeholders are resolved -> listing is fully materialized.
            if cards_count > 0 and placeholders_count == 0:
                zero_placeholders_streak += 1
                if zero_placeholders_streak >= 2:
                    log.info(
                        "yandex.scroll.stop_placeholders_done",
                        cards=best_count,
                        placeholders=placeholders_count,
                    )
                    break
            else:
                zero_placeholders_streak = 0

            now = time.monotonic()
            idle_for = now - last_new
            total_for = now - start
            if idle_for >= self._SCROLL_IDLE_SECONDS:
                log.info(
                    "yandex.scroll.stop_idle",
                    idle_seconds=round(idle_for, 1),
                    cards=best_count,
                    placeholders=placeholders_count,
                )
                break
            if total_for >= self._SCROLL_MAX_SECONDS:
                log.warning(
                    "yandex.scroll.stop_max_time",
                    total_seconds=round(total_for, 1),
                    cards=best_count,
                    placeholders=placeholders_count,
                )
                break

            await self._scroll_results_panel(page)
            await asyncio.sleep(self._SCROLL_STEP_PAUSE_SECONDS)

        return await self._find_cards(page)

    async def _scroll_results_panel(self, page: Page) -> None:
        list_el = await page.query_selector(".search-list-view__list")
        if list_el:
            scrolled = await list_el.evaluate(
                """el => {
                    const findScrollable = (node) => {
                        let cur = node;
                        while (cur) {
                            if (cur.scrollHeight > cur.clientHeight + 2) return cur;
                            cur = cur.parentElement;
                        }
                        return null;
                    };
                    const target = findScrollable(el);
                    if (!target) return false;
                    const before = target.scrollTop;
                    const delta = Math.max(target.clientHeight * 0.9, 700);
                    target.scrollBy(0, delta);
                    if (Math.abs(target.scrollTop - before) < 2) {
                        target.scrollTo(0, target.scrollHeight);
                    }
                    return true;
                }"""
            )
            if scrolled:
                return

        panel_selectors = [".sidebar-view__panel", "[class*='scroll__container']", "[class*='search-list-view']"]
        for selector in panel_selectors:
            panel = await page.query_selector(selector)
            if panel:
                await panel.evaluate("el => el.scrollBy(0, Math.max(el.clientHeight * 0.9, 700))")
                return
        await page.mouse.wheel(0, 1200)

    def _build_search_urls(self, keyword: str) -> list[str]:
        geo_variants = ["Омск", "Омская область"]
        region_name = (settings.region or "").strip()
        if region_name and region_name not in geo_variants:
            geo_variants.insert(0, region_name)

        queries: list[str] = []
        keyword_lower = keyword.lower()
        for geo in geo_variants:
            if geo.lower() in keyword_lower:
                queries.append(keyword)
            else:
                queries.append(f"{keyword} {geo}")
        # Keep non-suffixed query as fallback.
        queries.append(keyword)

        urls: list[str] = []
        for query in queries:
            encoded = quote_plus(query)
            urls.append(f"{YANDEX_MAPS_ROOT}?text={encoded}")

        region = str(settings.yandex_region_code or "").strip("/")
        if region:
            for query in queries:
                encoded = quote_plus(query)
                urls.append(f"{YANDEX_MAPS_ROOT}{region}/?text={encoded}")
        # Keep order, remove duplicates.
        return list(dict.fromkeys(urls))

    async def _find_cards(self, page: Page):
        """Find business snippet cards, filtering out nested inner elements (from Denis)."""
        cards = await page.evaluate_handle("""() => {
            const all = document.querySelectorAll('.search-business-snippet-view, li.search-snippet-view');
            const result = [];
            for (const el of all) {
                // Skip if nested inside another snippet
                const parent = el.parentElement?.closest('.search-business-snippet-view, li.search-snippet-view');
                if (parent) continue;
                // Must have a title to be a real company card
                const title = el.querySelector('.search-business-snippet-view__title') || el.querySelector('h2');
                if (title) result.push(el);
            }
            return result;
        }""")

        length = await cards.evaluate("arr => arr.length")
        if length > 0:
            elements = []
            for i in range(length):
                el = await cards.evaluate_handle(f"arr => arr[{i}]")
                elements.append(el.as_element())
            log.debug("yandex.find_cards.filtered", total_raw=length)
            return elements

        # Fallback: simple selectors
        await asyncio.sleep(1)
        for selector in ["li.search-snippet-view", "[class*='search-snippet']"]:
            cards_list = await page.query_selector_all(selector)
            if cards_list:
                return cards_list
        return []

    async def _results_stats(self, page: Page) -> tuple[int, int]:
        cards = await page.query_selector_all("li.search-snippet-view")
        placeholders = await page.query_selector_all(".search-snippet-view__placeholder")
        return len(cards), len(placeholders)

    async def _wait_results_ready(self, page: Page) -> None:
        selectors = [".search-list-view__list", "li.search-snippet-view", ".search-snippet-view__placeholder"]
        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=15_000)
                return
            except Exception:
                continue

    async def _is_bot_challenge_page(self, page: Page) -> bool:
        title = (await page.title()).strip().lower()
        if "подтвердите, что запросы отправляли вы" in title:
            return True
        body = ((await page.content()) or "").lower()
        needles = (
            "подтвердите, что запросы отправляли вы, а не робот",
            "запросы с вашего устройства похожи на автоматические",
            "please confirm that the requests were sent by you",
            "requests from your device look automated",
        )
        return any(n in body for n in needles)

    async def _parse_card(self, page: Page, card, keyword: str) -> RawCompany | None:
        name_el = await card.query_selector(".search-business-snippet-view__title")
        if not name_el:
            name_el = await card.query_selector("[class*='orgcard-header__name']")
        if not name_el:
            name_el = await card.query_selector("h2")
        name_raw = (await name_el.inner_text()).strip() if name_el else None

        # Rating (e.g. "4,0" or aria-label "Оценка 4 Из 5")
        rating_text = ""
        rating_el = await card.query_selector(".business-rating-badge-view__rating-text")
        if rating_el:
            rating_text = (await rating_el.inner_text()).strip()
        if not rating_text:
            # Fallback from Denis: broader selector
            rating_el = await card.query_selector("[class*='rating-badge-view__rating-text']")
            if not rating_el:
                rating_el = await card.query_selector("[class*='rating-badge-view__rating']")
            if rating_el:
                rating_text = (await rating_el.inner_text()).strip()
        if not rating_text:
            stars_el = await card.query_selector(".business-rating-badge-view__stars")
            if stars_el:
                rating_text = (await stars_el.get_attribute("aria-label") or "").strip()
        average_rating = parse_float(rating_text)

        # Reviews count (e.g. "27 оценок")
        reviews_text = ""
        reviews_el = await card.query_selector(".business-rating-amount-view")
        if reviews_el:
            reviews_text = (await reviews_el.inner_text()).strip()
        if not reviews_text:
            reviews_el = await card.query_selector(".business-rating-with-text-view__count")
            if not reviews_el:
                reviews_el = await card.query_selector("[class*='rating-with-text-view__count']")
            if reviews_el:
                reviews_text = (await reviews_el.inner_text()).strip()
        reviews_count = parse_int(reviews_text)

        # Address
        addr_el = await card.query_selector(".search-business-snippet-view__address")
        if not addr_el:
            addr_el = await card.query_selector("[class*='snippet-view__address']")
        if not addr_el:
            addr_el = await card.query_selector("a[href*='/house/']")
        addresses = []
        if addr_el:
            addr_text = (await addr_el.inner_text()).strip()
            if addr_text:
                addresses = [addr_text]

        # Phone
        phone_el = await card.query_selector("[class*='phone']")
        phones = []
        if phone_el:
            phone_text = (await phone_el.inner_text()).strip()
            if phone_text:
                phones = [phone_text]

        # Source link
        link_el = await card.query_selector("a.link-overlay[href*='/maps/org/']")
        if not link_el:
            link_el = await card.query_selector("a[href*='/org/']")
        source_link = await link_el.get_attribute("href") if link_el else None
        if source_link and not source_link.startswith("http"):
            source_link = f"https://yandex.ru{source_link}"

        source_id = await self._card_source_id(card)
        if source_link:
            m = re.search(r"/org/[^/]+/(\d+)", source_link)
            if m:
                source_id = m.group(1)

        card_html = await card.inner_html()

        return RawCompany(
            source=self.source_name,
            source_id=source_id,
            source_link=source_link,
            name_raw=name_raw,
            phones=phones,
            addresses=addresses,
            average_rating=average_rating,
            reviews_count=reviews_count,
            raw_payload={"keyword": keyword, "card_html_snippet": card_html[:2000]},
        ) if name_raw else None

    async def _card_source_id(self, card) -> str | None:
        body = await card.query_selector("[data-id]")
        if not body:
            return None
        return await body.get_attribute("data-id")

    # ── Debug helpers ──────────────────────────────────────────────────────

    def _merge_state_into_dom_companies(
        self,
        dom_companies: list[RawCompany],
        state_companies: list[RawCompany],
    ) -> list[RawCompany]:
        if not state_companies:
            return dom_companies
        if not dom_companies:
            return state_companies

        state_by_id = {c.source_id: c for c in state_companies if c.source_id}
        state_by_link = {
            link: c
            for c in state_companies
            if (link := self._normalize_source_link(c.source_link))
        }

        merged: list[RawCompany] = []
        used_state_keys: set[str] = set()
        for dom in dom_companies:
            state = None
            if dom.source_id:
                state = state_by_id.get(dom.source_id)
            if not state:
                norm_link = self._normalize_source_link(dom.source_link)
                if norm_link:
                    state = state_by_link.get(norm_link)

            if not state:
                merged.append(dom)
                continue

            merged_company = self._merge_company_pair(dom, state)
            merged.append(merged_company)
            merge_key = self._company_merge_key(state)
            if merge_key:
                used_state_keys.add(merge_key)

        for state in state_companies:
            merge_key = self._company_merge_key(state)
            if merge_key and merge_key in used_state_keys:
                continue

            matched = False
            for dom in dom_companies:
                if dom.source_id and state.source_id and dom.source_id == state.source_id:
                    matched = True
                    break
                dom_link = self._normalize_source_link(dom.source_link)
                state_link = self._normalize_source_link(state.source_link)
                if dom_link and state_link and dom_link == state_link:
                    matched = True
                    break

            if not matched:
                merged.append(state)
        return merged

    def _company_merge_key(self, company: RawCompany) -> str | None:
        if company.source_id:
            return f"id:{company.source_id}"
        norm_link = self._normalize_source_link(company.source_link)
        if norm_link:
            return f"link:{norm_link}"
        return None

    def _merge_company_pair(self, dom: RawCompany, state: RawCompany) -> RawCompany:
        return RawCompany(
            source=dom.source or state.source,
            source_id=dom.source_id or state.source_id,
            source_link=dom.source_link or state.source_link,
            name_raw=dom.name_raw or state.name_raw,
            raw_payload=self._merge_dicts_preferring_dom(dom.raw_payload, state.raw_payload),
            phones=self._merge_unique(dom.phones, state.phones),
            emails=self._merge_unique(dom.emails, state.emails),
            addresses=self._merge_unique(dom.addresses, state.addresses),
            contacts_json=self._merge_contacts_json(dom.contacts_json, state.contacts_json),
            inn=dom.inn or state.inn,
            ogrn=dom.ogrn or state.ogrn,
            average_rating=dom.average_rating if dom.average_rating is not None else state.average_rating,
            reviews_count=self._merge_reviews_count(dom.reviews_count, state.reviews_count),
            collected_at=dom.collected_at,
            reviews=dom.reviews or state.reviews,
        )

    def _merge_reviews_count(self, dom_count: int | None, state_count: int | None) -> int | None:
        values = [v for v in (dom_count, state_count) if v is not None]
        if not values:
            return None
        return max(values)

    def _merge_dicts_preferring_dom(self, dom_payload: dict | None, state_payload: dict | None) -> dict:
        out = dict(state_payload or {})
        out.update(dom_payload or {})
        return out

    def _merge_contacts_json(self, dom_contacts: dict | None, state_contacts: dict | None) -> dict:
        dom_contacts = dom_contacts or {}
        state_contacts = state_contacts or {}
        out = dict(state_contacts)
        for key, dom_value in dom_contacts.items():
            state_value = out.get(key)
            if isinstance(dom_value, list):
                out[key] = self._merge_unique(dom_value, state_value if isinstance(state_value, list) else [])
                continue
            if isinstance(dom_value, dict):
                merged_nested = dict(state_value) if isinstance(state_value, dict) else {}
                merged_nested.update(dom_value)
                out[key] = merged_nested
                continue
            if dom_value is not None:
                out[key] = dom_value
        return out

    def _merge_unique(self, first: list[str] | None, second: list[str] | None) -> list[str]:
        out: list[str] = []
        for value in (first or []) + (second or []):
            text = str(value or "").strip()
            if not text:
                continue
            if text not in out:
                out.append(text)
        return out

    async def _collect_reviews_for_listing(self, page: Page, companies: list[RawCompany]) -> None:
        if not companies:
            return

        max_attempts = max(1, settings.yandex_reviews_max_attempts)
        success_count = 0
        attempted_count = 0

        for idx, company in enumerate(companies, start=1):
            log.info(
                "yandex.reviews.company.start",
                idx=idx,
                total=len(companies),
                source_id=company.source_id,
                name=company.name_raw,
            )
            if not self._should_open_reviews(company):
                log.info(
                    "yandex.reviews.company.skip",
                    reason="no_reviews_on_listing",
                    source_id=company.source_id,
                    name=company.name_raw,
                    reviews_count=company.reviews_count,
                )
                company.reviews = []
                continue

            attempted_count += 1
            reviews: list[RawReview] = []

            # Strategy 1: listing-click flow (existing)
            try:
                reviews = await self._try_listing_click_reviews(page, company, idx)
            except Exception as exc:
                log.warning(
                    "yandex.reviews.listing_click.error",
                    source_id=company.source_id,
                    name=company.name_raw,
                    error=str(exc),
                    reason="company_open_failed",
                )

            # Strategy 2: direct /reviews/ URL fallback (if listing-click yielded nothing)
            if not reviews and company.source_link:
                for attempt in range(1, max_attempts + 1):
                    try:
                        reviews = await self._try_direct_reviews_url(page, company)
                        if reviews:
                            break
                    except BotChallengeError:
                        log.warning(
                            "yandex.reviews.direct_url.bot_challenge",
                            source_id=company.source_id,
                            attempt=attempt,
                            reason="bot_challenge",
                        )
                        break  # Don't retry on bot challenge
                    except Exception as exc:
                        log.warning(
                            "yandex.reviews.direct_url.error",
                            source_id=company.source_id,
                            attempt=attempt,
                            error=str(exc),
                            reason="reviews_dom_empty" if "dom" in str(exc).lower() else "reviews_tab_failed",
                        )
                        if attempt >= max_attempts:
                            break
                        await asyncio.sleep(1.0)

            if reviews:
                company.reviews = reviews
                company.reviews_count = max(company.reviews_count or 0, len(reviews))
                ratings = [r.rating for r in reviews if r.rating is not None]
                if ratings:
                    avg = round(sum(ratings) / len(ratings), 2)
                    if company.average_rating is None or len(ratings) >= 5:
                        company.average_rating = avg
                success_count += 1
                log.info(
                    "yandex.reviews.company.done",
                    source_id=company.source_id,
                    name=company.name_raw,
                    reviews=len(reviews),
                )
            else:
                company.reviews = []
                log.warning(
                    "yandex.reviews.company.empty",
                    source_id=company.source_id,
                    name=company.name_raw,
                    reviews_count=company.reviews_count,
                    reason="reviews_dom_empty",
                )

        # Log extraction success ratio
        if attempted_count > 0:
            ratio = success_count / attempted_count
            log.info(
                "yandex.reviews.extraction_ratio",
                success=success_count,
                attempted=attempted_count,
                ratio=round(ratio, 2),
            )

    async def _try_listing_click_reviews(
        self, page: Page, company: RawCompany, idx: int
    ) -> list[RawReview]:
        """Try the existing listing-click flow to get reviews."""
        opened = await self._open_company_from_listing(page, company, preferred_index=idx - 1)
        if not opened:
            return []

        tab_opened = await self._open_reviews_tab_with_retry(page, company)
        if not tab_opened:
            return []

        await self._scroll_reviews_until_end(page)
        return await self._extract_reviews_from_open_company(page, company.source_link)

    async def _try_direct_reviews_url(self, page: Page, company: RawCompany) -> list[RawReview]:
        """Navigate directly to a company's /reviews/ URL and extract reviews."""
        reviews_url = self._build_reviews_url(company)
        if not reviews_url:
            return []

        log.info(
            "yandex.reviews.direct_url.start",
            source_id=company.source_id,
            url=reviews_url,
        )

        try:
            resp = await self._goto_with_fallback(
                page,
                reviews_url,
                timeout_ms=30_000,
                purpose="reviews_direct",
            )
        except Exception:
            return []

        if resp and resp.status and resp.status >= 400:
            return []

        if await self._is_bot_challenge_page(page):
            raise BotChallengeError("Bot challenge on direct reviews URL")

        await asyncio.sleep(2)

        # Wait for reviews content to load
        await self._wait_reviews_section_visible(page)
        await self._scroll_reviews_until_end(page)

        # Extract via DOM
        reviews = await self._extract_reviews_from_open_company(page, company.source_link)

        # Fallback: try state-view JSON extraction if DOM yielded nothing
        if not reviews:
            reviews = await self._extract_reviews_from_state_view(page, company.source_link)

        return reviews

    def _build_reviews_url(self, company: RawCompany) -> str | None:
        """Build a direct /reviews/ URL from company source_link or source_id."""
        if company.source_link:
            link = company.source_link.rstrip("/")
            if "/reviews" not in link:
                return f"{link}/reviews/"
            return link
        if company.source_id:
            return f"{YANDEX_MAPS_ROOT}org/{company.source_id}/reviews/"
        return None

    async def _extract_reviews_from_state_view(
        self, page: Page, company_link: str | None
    ) -> list[RawReview]:
        """Try to extract reviews from Yandex state-view JSON on the page."""
        try:
            script = await page.query_selector("script.state-view[type='application/json']")
            if not script:
                return []

            raw = (await script.inner_text()).strip()
            state = json.loads(raw)
        except Exception:
            return []

        # Navigate the state structure to find reviews
        reviews: list[RawReview] = []
        try:
            stack = state.get("stack") or []
            for frame in stack:
                if not isinstance(frame, dict):
                    continue
                # Look for review data in various locations
                for key in ("reviews", "businessReviews", "results"):
                    items = frame.get(key)
                    if not isinstance(items, (list, dict)):
                        continue
                    if isinstance(items, dict):
                        items = items.get("items") or items.get("reviews") or []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        text = (item.get("text") or item.get("body") or "").strip()
                        if not text:
                            continue
                        rating_raw = item.get("rating")
                        author_raw = item.get("author") or item.get("user")
                        author = None
                        if isinstance(author_raw, dict):
                            author = author_raw.get("name") or author_raw.get("displayName")
                        elif isinstance(author_raw, str):
                            author = author_raw

                        review_date = self._parse_review_date(
                            str(item.get("updatedTime") or item.get("createdTime") or "")
                        )

                        reviews.append(
                            RawReview(
                                source=self.source_name,
                                text=text[:500],
                                rating=float(rating_raw) if rating_raw is not None else None,
                                author=author,
                                review_date=review_date,
                                source_link=company_link,
                            )
                        )
        except Exception as exc:
            log.debug("yandex.reviews.state_view_extract.error", error=str(exc))

        return self._dedupe_reviews(reviews)

    def _should_open_reviews(self, company: RawCompany) -> bool:
        """Open the reviews tab when listing metadata says reviews exist,
        or when we have a source link (attempt even without reviews_count)."""
        if company.reviews:
            return False
        if company.reviews_count and company.reviews_count > 0:
            return True
        # Relaxed gating: attempt if we have a source link, even without reviews_count
        if company.source_link:
            return True
        return False

    async def _open_company_from_listing(
        self,
        page: Page,
        company: RawCompany,
        preferred_index: int,
    ) -> bool:
        for _ in range(self._REVIEWS_TAB_MAX_ATTEMPTS):
            card = await self._find_matching_card(page, company, preferred_index)
            if not card:
                await asyncio.sleep(0.6)
                continue
            try:
                await card.scroll_into_view_if_needed(timeout=4_000)
            except Exception:
                pass

            selectors = [
                ".search-snippet-view__body-button-wrapper",
                ".search-snippet-view__body",
                "a.link-overlay[href*='/maps/org/']",
            ]
            clicked = False
            for selector in selectors:
                clickable = await card.query_selector(selector)
                if not clickable:
                    continue
                try:
                    await clickable.click(timeout=8_000)
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                try:
                    await card.click(timeout=8_000)
                    clicked = True
                except Exception:
                    clicked = False

            if clicked and await self._wait_company_panel_opened(page):
                return True
            await asyncio.sleep(0.8)
        return False

    async def _find_matching_card(self, page: Page, company: RawCompany, preferred_index: int):
        cards = await self._find_cards(page)
        if not cards:
            return None

        company_link = self._normalize_source_link(company.source_link)
        company_name = (company.name_raw or "").strip().lower()
        best_score = -1
        best_card = None

        for card in cards:
            score = 0
            card_source_id = await self._card_source_id(card)
            if company.source_id and card_source_id and company.source_id == card_source_id:
                score += 100

            card_link = await self._card_source_link(card)
            if company_link and card_link and company_link == card_link:
                score += 50

            card_name = await self._card_name(card)
            if company_name and card_name and company_name == card_name:
                score += 20

            if score > best_score:
                best_score = score
                best_card = card

        if best_score > 0:
            return best_card
        if 0 <= preferred_index < len(cards):
            return cards[preferred_index]
        return best_card

    async def _card_source_link(self, card) -> str | None:
        link_el = await card.query_selector("a.link-overlay[href*='/maps/org/']")
        if not link_el:
            link_el = await card.query_selector("a[href*='/org/']")
        href = await link_el.get_attribute("href") if link_el else None
        if href and not href.startswith("http"):
            href = f"https://yandex.ru{href}"
        return self._normalize_source_link(href)

    async def _card_name(self, card) -> str:
        name_el = await card.query_selector(".search-business-snippet-view__title")
        if not name_el:
            name_el = await card.query_selector("h2")
        if not name_el:
            return ""
        return (await name_el.inner_text()).strip().lower()

    def _normalize_source_link(self, source_link: str | None) -> str | None:
        if not source_link:
            return None
        link = source_link.strip()
        if not link:
            return None
        if not link.startswith("http"):
            if link.startswith("/"):
                link = f"https://yandex.ru{link}"
            else:
                link = f"https://yandex.ru/{link}"
        link = re.sub(r"[?#].*$", "", link)
        link = re.sub(r"^https?://", "https://", link, flags=re.IGNORECASE)
        return link.rstrip("/")

    async def _wait_company_panel_opened(self, page: Page) -> bool:
        selectors = [
            ".tabs-select-view",
            ".tabs-select-view__title._name_reviews",
            "a[href*='/reviews/']",
        ]
        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=8_000)
                return True
            except Exception:
                continue
        return False

    async def _open_reviews_tab_with_retry(self, page: Page, company: RawCompany) -> bool:
        for attempt in range(1, self._REVIEWS_TAB_MAX_ATTEMPTS + 1):
            if await self._is_reviews_tab_open(page) or await self._is_reviews_empty_state(page):
                return True

            selectors = [
                ".tabs-select-view__title._name_reviews",
                ".tabs-select-view__title._name_reviews a",
                "a.tabs-select-view__label[href*='/reviews/']",
                "a[href*='/reviews/']",
                "[role='tab'][aria-label*='Отзывы']",
                "[role='tab'][aria-label*='отзывы']",
            ]
            clicked = False
            for selector in selectors:
                tab = await page.query_selector(selector)
                if not tab:
                    continue
                try:
                    await tab.click(timeout=8_000)
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                clicked = await self._click_reviews_tab_by_text(page)

            if (
                (clicked and await self._wait_reviews_section_visible(page))
                or await self._is_reviews_tab_open(page)
                or await self._is_reviews_empty_state(page)
            ):
                return True

            log.warning(
                "yandex.reviews.tab_retry",
                attempt=attempt,
                max_attempts=self._REVIEWS_TAB_MAX_ATTEMPTS,
                source_id=company.source_id,
                name=company.name_raw,
            )
            await asyncio.sleep(1.0)
        return await self._is_reviews_tab_open(page) or await self._is_reviews_empty_state(page)

    async def _click_reviews_tab_by_text(self, page: Page) -> bool:
        try:
            return bool(
                await page.evaluate(
                    """() => {
                        const isReviews = (el) => {
                            const text = (el?.textContent || '').toLowerCase();
                            const aria = (el?.getAttribute?.('aria-label') || '').toLowerCase();
                            return text.includes('отзыв') || aria.includes('отзыв');
                        };
                        const tabs = Array.from(
                            document.querySelectorAll(
                                '.tabs-select-view__title, [role="tab"], a.tabs-select-view__label, a[href*="/reviews/"]'
                            )
                        );
                        const target = tabs.find(isReviews);
                        if (!target) return false;
                        target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
                        return true;
                    }"""
                )
            )
        except Exception:
            return False

    async def _is_reviews_tab_open(self, page: Page) -> bool:
        selected = await page.query_selector(".tabs-select-view__title._name_reviews._selected")
        if selected:
            return True
        try:
            selected_reviews = await page.evaluate(
                """() => {
                    const selected =
                        document.querySelector('.tabs-select-view__title._selected')
                        || document.querySelector('[role="tab"][aria-selected="true"]');
                    if (!selected) return false;
                    const text = (selected.textContent || '').toLowerCase();
                    const aria = (selected.getAttribute('aria-label') || '').toLowerCase();
                    return text.includes('отзыв') || aria.includes('отзыв');
                }"""
            )
            if selected_reviews:
                return True
        except Exception:
            pass
        return "/reviews/" in (page.url or "")

    async def _wait_reviews_section_visible(self, page: Page) -> bool:
        selectors = [
            ".business-review-view",
            ".business-reviews-view",
            ".business-review-view__info",
            ".tabs-select-view__title._name_reviews._selected",
            ".card-reviews-view._empty-tab",
            ".tab-empty-view",
            "[data-chunk='reviews']",
        ]
        for selector in selectors:
            try:
                await page.wait_for_selector(selector, timeout=8_000)
                return True
            except Exception:
                continue
        return False

    async def _is_reviews_empty_state(self, page: Page) -> bool:
        selectors = [
            ".card-reviews-view._empty-tab",
            ".tab-empty-view",
            "h2.tab-empty-view__title",
        ]
        for selector in selectors:
            node = await page.query_selector(selector)
            if node:
                return True
        try:
            return bool(
                await page.evaluate(
                    """() => {
                        const title = document.querySelector('h2.tab-empty-view__title');
                        const text = (title?.textContent || '').toLowerCase().trim();
                        return text.includes('будьте первым');
                    }"""
                )
            )
        except Exception:
            return False

    async def _scroll_reviews_until_end(self, page: Page) -> None:
        start = time.monotonic()
        last_count = -1
        stable_steps = 0

        while True:
            if await self._is_reviews_empty_state(page):
                log.info("yandex.reviews.scroll.stop_empty")
                return

            reviews_count = await self._reviews_count(page)
            if reviews_count == last_count:
                stable_steps += 1
            else:
                stable_steps = 0
                last_count = reviews_count

            if stable_steps >= self._REVIEWS_SCROLL_STABLE_STEPS:
                log.info(
                    "yandex.reviews.scroll.stop_stable",
                    reviews=reviews_count,
                    stable_steps=stable_steps,
                )
                return

            elapsed = time.monotonic() - start
            if elapsed >= self._REVIEWS_SCROLL_MAX_SECONDS:
                log.warning(
                    "yandex.reviews.scroll.stop_max_time",
                    reviews=reviews_count,
                    total_seconds=round(elapsed, 1),
                )
                return

            scrolled = await page.evaluate(
                """() => {
                    const review = document.querySelector('.business-review-view');
                    const findScrollable = (node) => {
                        let cur = node;
                        while (cur) {
                            if (cur.scrollHeight > cur.clientHeight + 2) return cur;
                            cur = cur.parentElement;
                        }
                        return null;
                    };
                    const target =
                        findScrollable(review)
                        || findScrollable(document.querySelector('.business-reviews-view'))
                        || findScrollable(document.querySelector('.tabs-select-view'))
                        || findScrollable(document.body);
                    if (!target) return false;
                    const before = target.scrollTop;
                    const delta = Math.max(target.clientHeight * 0.9, 700);
                    target.scrollBy(0, delta);
                    if (Math.abs(target.scrollTop - before) < 2) {
                        target.scrollTo(0, target.scrollHeight);
                    }
                    return true;
                }"""
            )
            if not scrolled:
                await page.mouse.wheel(0, 1200)
            await asyncio.sleep(self._REVIEWS_SCROLL_STEP_PAUSE_SECONDS)

    async def _reviews_count(self, page: Page) -> int:
        reviews = await page.query_selector_all(".business-review-view")
        return len(reviews)

    async def _extract_reviews_from_open_company(
        self,
        page: Page,
        company_link: str | None,
    ) -> list[RawReview]:
        rows = await page.evaluate(
            """() => {
                const out = [];
                const nodes = document.querySelectorAll('.business-review-view');
                for (const node of nodes) {
                    const body = node.querySelector('.business-review-view__body');
                    const authorNode =
                        node.querySelector('.business-review-view__author-name [itemprop="name"]')
                        || node.querySelector('.business-review-view__author-name');
                    const ratingMeta = node.querySelector('meta[itemprop="ratingValue"]');
                    const stars = node.querySelector('.business-rating-badge-view__stars');
                    const dateMeta = node.querySelector('meta[itemprop="datePublished"]');
                    const reviewLink = node.querySelector('a[href*="/reviews/"]');
                    out.push({
                        text: body ? (body.innerText || '').trim() : '',
                        author: authorNode ? (authorNode.textContent || '').trim() : '',
                        rating: ratingMeta?.content || stars?.getAttribute('aria-label') || '',
                        review_date: dateMeta?.content || '',
                        source_link: reviewLink?.href || '',
                    });
                }
                return out;
            }"""
        )

        reviews: list[RawReview] = []
        for row in rows:
            review = self._raw_review_from_dom_item(row, company_link)
            if review:
                reviews.append(review)
        return self._dedupe_reviews(reviews)

    def _raw_review_from_dom_item(self, row: dict, company_link: str | None) -> RawReview | None:
        if not isinstance(row, dict):
            return None

        text = str(row.get("text") or "").strip() or None
        author = str(row.get("author") or "").strip() or None
        rating = parse_float(str(row.get("rating") or ""))
        review_date = self._parse_review_date(str(row.get("review_date") or "").strip())

        source_link = str(row.get("source_link") or "").strip() or None
        if not source_link:
            source_link = company_link
        if source_link and not source_link.startswith("http") and source_link.startswith("/"):
            source_link = f"https://yandex.ru{source_link}"

        if not text and not author and rating is None:
            return None

        return RawReview(
            source=self.source_name,
            text=text,
            rating=rating,
            author=author,
            review_date=review_date,
            source_link=source_link,
        )

    def _parse_review_date(self, value: str) -> datetime | None:
        if not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed

    def _dedupe_reviews(self, reviews: list[RawReview]) -> list[RawReview]:
        out: list[RawReview] = []
        seen: set[str] = set()
        for review in reviews:
            key = "|".join([
                (review.author or "").strip().lower(),
                review.review_date.isoformat() if review.review_date else "",
                (review.text or "").strip().lower()[:160],
            ])
            if key in seen:
                continue
            seen.add(key)
            out.append(review)
        return out

    async def _attach_debug_hooks(self, page: Page) -> None:
        if not settings.yandex_debug_browser:
            return

        page.on(
            "console",
            lambda msg: log.info("yandex.debug.console", type=msg.type, text=msg.text[:400]),
        )
        page.on(
            "requestfailed",
            lambda req: log.warning(
                "yandex.debug.request_failed",
                url=req.url,
                method=req.method,
                error=req.failure,
            ),
        )

    async def _debug_screenshot(self, page: Page, stage: str) -> None:
        if not settings.yandex_debug_browser:
            return
        target = Path(settings.yandex_debug_screenshot_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        stem = target.stem
        suffix = target.suffix or ".png"
        shot = target.with_name(f"{stem}_{stage}{suffix}")
        try:
            await page.screenshot(path=str(shot), full_page=True)
            log.info("yandex.debug.screenshot", stage=stage, path=str(shot))
        except Exception as exc:
            log.warning("yandex.debug.screenshot_error", stage=stage, error=str(exc))

    async def _debug_hold(self, stage: str) -> None:
        if not settings.yandex_debug_browser:
            return
        hold = max(0.0, float(settings.yandex_debug_hold_seconds))
        if hold <= 0:
            return
        log.info("yandex.debug.hold", stage=stage, seconds=hold)
        await asyncio.sleep(hold)


def _exc_detail(exc: Exception) -> str:
    return str(exc)
