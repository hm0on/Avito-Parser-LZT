"""FSSP (Federal Bailiff Service) web scraper."""

from __future__ import annotations

import asyncio
import json
from html import unescape
from urllib.parse import quote, unquote, urlencode, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from src.config import settings
from src.enrichment.registries.base import AbstractChecker, CheckResult
from src.proxy import enrichment_proxy_manager, iter_proxy_urls

log = structlog.get_logger(__name__)

_FSSP_URL = "https://fssp.gov.ru/iss/ip/"
_FSSP_SEARCH_URL = "https://is-go.fssp.gov.ru/ajax_search"
_REGION_ALL = "0"
_PROCESSING_ERROR = "FSSP query is still processing after repeated retries"
_CAPTCHA_ERROR = "FSSP requires captcha confirmation for this proxy"
_RESULT_TABLE_SELECTORS = ("table#resultList", "table.b-result-table", "table")
_NOT_FOUND_MARKERS = (
    "записей не найдено",
    "по вашему запросу ничего не найдено",
    "ничего не найдено",
)
_PROCESSING_MARKERS = (
    "ваш запрос обрабатывается",
    "попробуйте позже",
)
_CAPTCHA_MARKERS = (
    "captcha-popup",
    "smartcaptcha",
    'id="captcha"',
    "ncapcha",
    "введите код с картинки",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://fssp.gov.ru/",
}


class FsspChecker(AbstractChecker):
    registry_name = "fssp"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
    ) -> CheckResult:
        if not inn:
            return CheckResult(registry=self.registry_name, found=False, error="INN required for FSSP")

        try:
            result = await self._try_httpx(inn=inn)
            if result is not None:
                return result
        except RuntimeError as exc:
            return CheckResult(registry=self.registry_name, found=False, error=str(exc))

        log.info("fssp.fallback_to_playwright", inn=inn)
        try:
            return await self._try_playwright(inn=inn)
        except RuntimeError as exc:
            return CheckResult(registry=self.registry_name, found=False, error=str(exc))

    async def _try_httpx(self, *, inn: str) -> CheckResult | None:
        params = _build_search_params(inn=inn)
        result_url = _build_result_page_url(inn=inn)
        pending_retries = max(0, int(settings.fssp_processing_max_retries))
        delay_seconds = max(1, int(settings.fssp_processing_retry_delay_seconds))
        captcha_seen = False
        parse_failed = False

        for attempt in range(pending_retries + 1):
            pending_seen = False
            for proxy_url in _iter_fssp_proxy_urls():
                try:
                    async with httpx.AsyncClient(
                        timeout=30.0,
                        follow_redirects=True,
                        headers=_httpx_headers(),
                        proxy=proxy_url,
                    ) as client:
                        html = await _submit_httpx_search(client, params=params)

                        if _has_processing_pending(html):
                            pending_seen = True
                            log.info(
                                "fssp.httpx.processing_wait",
                                inn=inn,
                                attempt=attempt + 1,
                                retry_in_seconds=delay_seconds,
                                proxy=proxy_url,
                            )
                            break

                        if _has_captcha(html):
                            captcha_seen = True
                            log.info("fssp.httpx.captcha_detected", proxy=proxy_url)
                            continue

                        parsed = _parse_html(html, self.registry_name)
                        if parsed.error != "could not parse FSSP response":
                            return parsed
                        parse_failed = True

                        follow_up_html = await _fetch_httpx_result_page(client, url=result_url)
                        if _has_processing_pending(follow_up_html):
                            pending_seen = True
                            log.info(
                                "fssp.httpx.processing_wait",
                                inn=inn,
                                attempt=attempt + 1,
                                retry_in_seconds=delay_seconds,
                                proxy=proxy_url,
                            )
                            break

                        if _has_captcha(follow_up_html):
                            captcha_seen = True
                            log.info("fssp.httpx.captcha_detected", proxy=proxy_url)
                            continue

                        parsed = _parse_html(follow_up_html, self.registry_name)
                        if parsed.error == "could not parse FSSP response":
                            parse_failed = True
                            log.info("fssp.httpx.unparseable_fallback", proxy=proxy_url)
                            continue
                        return parsed

                except Exception as exc:
                    log.warning("fssp.httpx.error", error=f"{type(exc).__name__}: {exc!r}", proxy=proxy_url)

            if not pending_seen:
                if captcha_seen:
                    return CheckResult(registry=self.registry_name, found=False, error=_CAPTCHA_ERROR)
                if parse_failed:
                    return CheckResult(
                        registry=self.registry_name,
                        found=False,
                        error="could not parse FSSP response",
                    )
                return None
            if attempt >= pending_retries:
                return CheckResult(registry=self.registry_name, found=False, error=_PROCESSING_ERROR)
            await asyncio.sleep(delay_seconds)

        return None

    async def _try_playwright(self, *, inn: str) -> CheckResult:
        last_error = ""
        pending_retries = max(0, int(settings.fssp_processing_max_retries))
        delay_seconds = max(1, int(settings.fssp_processing_retry_delay_seconds))
        result_url = _build_result_page_url(inn=inn)

        for proxy in _iter_fssp_playwright_proxies():
            try:
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(
                        headless=True,
                        proxy=proxy,
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                    try:
                        context = await browser.new_context(
                            user_agent=_HEADERS["User-Agent"],
                            locale="ru-RU",
                        )
                        page = await context.new_page()
                        await page.goto(_FSSP_URL, wait_until="domcontentloaded", timeout=30_000)
                        await page.check('input[name="is[variant]"][value="5"]')
                        await _select_if_exists(page, 'select[name="is[region_id][0]"]', _REGION_ALL)
                        await _fill_if_exists(page, 'input[name="is[inn]"]', inn)
                        await page.click('input.search-button, button[type="submit"], input[type="submit"]')
                        await page.wait_for_load_state("domcontentloaded", timeout=20_000)

                        for attempt in range(pending_retries + 1):
                            await asyncio.sleep(2)
                            html = await page.content()

                            if _has_processing_pending(html):
                                if attempt >= pending_retries:
                                    return CheckResult(
                                        registry=self.registry_name,
                                        found=False,
                                        error=_PROCESSING_ERROR,
                                    )
                                log.info(
                                    "fssp.playwright.processing_wait",
                                    inn=inn,
                                    attempt=attempt + 1,
                                    retry_in_seconds=delay_seconds,
                                    proxy=proxy.get("server"),
                                )
                                await asyncio.sleep(delay_seconds)
                                await page.goto(result_url, wait_until="domcontentloaded", timeout=30_000)
                                continue

                            if _has_captcha(html):
                                break

                            parsed = _parse_html(html, self.registry_name)
                            if parsed.error != "could not parse FSSP response":
                                return parsed
                            break
                    finally:
                        await browser.close()

            except Exception as exc:
                last_error = str(exc)
                log.warning("fssp.playwright.error", error=last_error, proxy=proxy.get("server"))

        return CheckResult(
            registry=self.registry_name,
            found=False,
            error=last_error or "could not parse FSSP response",
        )


def _build_search_params(*, inn: str) -> dict[str, str]:
    return {
        "page": "1",
        "system": "ip",
        "is[extended]": "1",
        "nocache": "1",
        "is[variant]": "5",
        "is[inn]": inn,
        "is[region_id][0]": _REGION_ALL,
    }


def _build_result_page_url(*, inn: str) -> str:
    return f"{_FSSP_URL}?{urlencode({'is[variant]': '5', 'is[inn]': inn})}"


def _httpx_headers() -> dict[str, str]:
    return {
        **_HEADERS,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": _FSSP_URL,
    }


def _has_captcha(html: str) -> bool:
    lower = (html or "").lower()
    return any(marker in lower for marker in _CAPTCHA_MARKERS)


def _has_processing_pending(html: str) -> bool:
    lower = _plain_text(html).lower()
    return all(marker in lower for marker in _PROCESSING_MARKERS)


def _plain_text(html: str) -> str:
    return BeautifulSoup(html or "", "lxml").get_text(separator=" ", strip=True)


def _parse_html(html: str, registry_name: str) -> CheckResult:
    html = _extract_payload_html(html)
    soup = BeautifulSoup(html, "lxml")

    table = (
        soup.select_one(_RESULT_TABLE_SELECTORS[0])
        or soup.select_one(_RESULT_TABLE_SELECTORS[1])
        or soup.select_one(_RESULT_TABLE_SELECTORS[2])
    )

    if not table:
        page_text = _plain_text(html).lower()
        if any(marker in page_text for marker in _NOT_FOUND_MARKERS):
            return CheckResult(registry=registry_name, found=False)
        return CheckResult(registry=registry_name, found=False, error="could not parse FSSP response")

    data_rows = [row for row in table.find_all("tr") if row.find("td")]
    if not data_rows:
        return CheckResult(registry=registry_name, found=False)

    items: list[dict] = []
    total_debt = 0.0

    for row in data_rows:
        cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
        items.append({"cells": cells})

        for cell in cells:
            cleaned = (
                cell.replace("\u00a0", "")
                .replace(" ", "")
                .replace(",", ".")
                .replace("руб.", "")
                .replace("СЂСѓР±.", "")
                .strip()
            )
            try:
                total_debt += float(cleaned)
            except ValueError:
                pass

    log.info("fssp.parse.done", executions=len(items), total_debt=total_debt)

    return CheckResult(
        registry=registry_name,
        found=True,
        status="has_executions",
        details={
            "executions_count": len(items),
            "total_debt_rub": round(total_debt, 2) if total_debt > 0 else None,
            "items_sample": items[:5],
        },
    )


async def _fill_if_exists(page, selector: str, value: str) -> None:
    try:
        await page.fill(selector, value)
    except Exception:
        pass


async def _select_if_exists(page, selector: str, value: str) -> None:
    try:
        await page.select_option(selector, value)
    except Exception:
        pass


async def _submit_httpx_search(client: httpx.AsyncClient, *, params: dict[str, str]) -> str:
    await client.get(_FSSP_URL)
    response = await client.get(_FSSP_SEARCH_URL, params=params)
    response.raise_for_status()
    return _extract_payload_html(_response_text(response))


async def _fetch_httpx_result_page(client: httpx.AsyncClient, *, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    return _response_text(response)


def _response_text(response: httpx.Response) -> str:
    content = response.content
    if b"windows-1251" in content[:4096].lower():
        return content.decode("cp1251", errors="ignore")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1251", errors="ignore")


def _extract_payload_html(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""

    candidates = [text]
    if text.startswith("(") and text.endswith(")"):
        candidates.append(text[1:-1])

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            html = payload.get("data")
            if isinstance(html, str):
                return unescape(html)

    return text


def _iter_fssp_proxy_urls():
    for proxy_url in iter_proxy_urls(enrichment_proxy_manager, purpose="fssp httpx"):
        yield _force_socks5_proxy(proxy_url)


def _iter_fssp_playwright_proxies():
    for proxy_url in _iter_fssp_proxy_urls():
        proxy = _playwright_proxy_from_url(proxy_url)
        if proxy:
            yield proxy


def _playwright_proxy_from_url(proxy_url: str) -> dict[str, str] | None:
    parsed = urlparse(_force_socks5_proxy(proxy_url))
    if not parsed.hostname or not parsed.port:
        return None

    proxy: dict[str, str] = {"server": f"socks5://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _force_socks5_proxy(proxy_url: str) -> str:
    raw = (proxy_url or "").strip()
    if not raw:
        return raw

    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        return raw

    auth = ""
    if parsed.username is not None:
        auth = quote(unquote(parsed.username), safe="")
        if parsed.password is not None:
            auth = f"{auth}:{quote(unquote(parsed.password), safe='')}"
        auth = f"{auth}@"

    return f"socks5://{auth}{parsed.hostname}:{parsed.port}"
