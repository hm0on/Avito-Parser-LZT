"""Yandex Maps collector — Playwright scraper with state-view JSON parsing."""

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

import structlog
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from src.collectors.base import AbstractCollector, RawCompany, RawReview, parse_float, parse_int
from src.config import settings
from src.proxy import proxy_manager

log = structlog.get_logger(__name__)

YANDEX_MAPS_ROOT = "https://yandex.ru/maps/"


class BotChallengeError(RuntimeError):
    """Raised when Yandex anti-bot challenge page is detected."""


class YandexCollector(AbstractCollector):
    source_name = "yandex"
    _SCROLL_IDLE_SECONDS = 30.0
    _SCROLL_STEP_PAUSE_SECONDS = 0.8
    _SCROLL_MAX_SECONDS = 240.0
    _max_concurrent_keywords = 0  # from config
    _uses_playwright = True

    async def collect(self, keyword: str) -> list[RawCompany]:
        urls = self._build_search_urls(keyword)
        log.info("yandex.collect.start", keyword=keyword, urls=urls)

        companies = await self._collect_urls(urls, keyword)
        log.info("yandex.collect.done", keyword=keyword, count=len(companies))
        return companies

    async def collect_from_url(self, url: str, *, keyword: str = "manual_url") -> list[RawCompany]:
        log.info("yandex.collect_url.start", keyword=keyword, url=url)
        companies = await self._collect_urls([url], keyword)
        log.info("yandex.collect_url.done", keyword=keyword, url=url, count=len(companies))
        return companies

    async def _collect_urls(self, urls: list[str], keyword: str) -> list[RawCompany]:
        if settings.yandex_single_pass:
            if not urls:
                return []
            if proxy_manager.count <= 0:
                raise RuntimeError("Proxy is required for Yandex but no proxies are configured")
            return await self._collect_once(urls[0], keyword, use_proxy=True)

        companies: list[RawCompany] = []
        for url in urls:
            try:
                companies = await self._collect_once(url, keyword, use_proxy=True)
                if companies:
                    break
            except Exception as exc:
                log.warning("yandex.collect.with_proxy_error", url=url, error=_exc_detail(exc))

        if not companies and proxy_manager.count > 0:
            log.info("yandex.collect.retry_without_proxy")
            for url in urls:
                try:
                    companies = await self._collect_once(url, keyword, use_proxy=False)
                    if companies:
                        break
                except Exception as exc:
                    log.warning("yandex.collect.without_proxy_error", url=url, error=_exc_detail(exc))
                    companies = []
        return companies

    async def _collect_once(self, url: str, keyword: str, *, use_proxy: bool) -> list[RawCompany]:
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
                    return await self._scrape_listing(page, url, keyword)
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
        proxy = proxy_manager.playwright_proxy() if use_proxy else None
        debug = settings.yandex_debug_browser
        slow_mo = max(0, int(settings.yandex_debug_slow_mo_ms)) if debug else 0
        log.info(
            "yandex.debug.launch",
            debug=debug,
            slow_mo=slow_mo,
            hold_s=settings.yandex_debug_hold_seconds,
            use_proxy=use_proxy,
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

    async def _scrape_listing(self, page: Page, url: str, keyword: str) -> list[RawCompany]:
        try:
            resp = await page.goto(url, wait_until="networkidle", timeout=45_000)
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
        cards = await self._collect_cards_with_scroll(page)
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
            except Exception as exc:
                log.warning("yandex.card.parse_error", error=str(exc))

        # After scroll-based loading, parse state-view JSON and prefer it.
        companies_from_state = await self._parse_state_view_companies(page, keyword)
        if companies_from_state:
            log.info(
                "yandex.listing.parsed_from_state_view_after_scroll",
                count=len(companies_from_state),
                dom_count=len(companies),
            )
            companies = companies_from_state

        await self._debug_screenshot(page, "after_parse")
        await self._debug_hold("after_parse")
        return companies

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

    async def _collect_cards_with_scroll(self, page: Page) -> list:
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
        region_name = (settings.region or "").strip()
        keyword_for_region = keyword
        if region_name and region_name.lower() not in keyword.lower():
            keyword_for_region = f"{keyword} {region_name}"

        regional_query = quote_plus(keyword_for_region)
        plain_query = quote_plus(keyword)
        urls: list[str] = [f"{YANDEX_MAPS_ROOT}?text={regional_query}"]
        # Fallback without region suffix if region-biased query returns nothing.
        urls.append(f"{YANDEX_MAPS_ROOT}?text={plain_query}")
        region = str(settings.yandex_region_code or "").strip("/")
        if region:
            urls.append(f"{YANDEX_MAPS_ROOT}{region}/?text={regional_query}")
            urls.append(f"{YANDEX_MAPS_ROOT}{region}/?text={plain_query}")
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
