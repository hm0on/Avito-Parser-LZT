"""ФССП (Federal Bailiff Service) web scraper.

Searches the public enforcement proceedings database at fssp.gov.ru.
Strategy:
  1. Try lightweight httpx form submission (fast, no browser overhead).
  2. If a CAPTCHA is detected, fall back to Playwright which renders JS and
     can interact with the search form normally.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from src.enrichment.registries.base import AbstractChecker, CheckResult
from src.proxy import proxy_manager

log = structlog.get_logger(__name__)

_FSSP_URL = "https://fssp.gov.ru/iss/ip/"
_REGION_ALL = "0"
_REGION_OMSK = "54"

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
        extra: dict | None = None,
    ) -> CheckResult:
        if not name and not inn:
            return CheckResult(registry=self.registry_name, found=False, error="name or INN required")

        # Fast path: plain HTTP form submission
        result = await self._try_httpx(inn=inn, name=name)
        if result is not None:
            return result

        # Slow path: full browser automation
        log.info("fssp.fallback_to_playwright", inn=inn, name=name)
        return await self._try_playwright(inn=inn, name=name)

    # ----------------------------------------------------------------- httpx path

    async def _try_httpx(self, *, inn: str | None, name: str | None) -> CheckResult | None:
        """Returns None if blocked by CAPTCHA so the caller can switch to Playwright."""
        form_data = _build_form(inn=inn, name=name)
        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                headers=_HEADERS,
            ) as client:
                # Establish session cookie
                await client.get(_FSSP_URL)
                resp = await client.post(_FSSP_URL, data=form_data)
                html = resp.text

            if _has_captcha(html):
                log.info("fssp.httpx.captcha_detected")
                return None

            return _parse_html(html, self.registry_name)

        except Exception as exc:
            log.warning("fssp.httpx.error", error=str(exc))
            return None

    # ----------------------------------------------------------------- Playwright path

    async def _try_playwright(self, *, inn: str | None, name: str | None) -> CheckResult:
        try:
            async with async_playwright() as pw:
                proxy = proxy_manager.playwright_proxy()
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

                    # Fill form fields for legal entity search
                    if inn:
                        await _fill_if_exists(page, 'input[name="is[inn]"]', inn)
                    elif name:
                        await _fill_if_exists(page, 'input[name="is[name]"]', name)

                    # nameType=1 → юридическое лицо / ИП
                    await _select_if_exists(page, 'select[name="is[nameType]"]', "1")

                    # Region: all regions when searching by INN, Omsk when by name
                    region = _REGION_ALL if inn else _REGION_OMSK
                    await _select_if_exists(page, 'select[name="is[region]"]', region)

                    # Submit and wait
                    await page.click('button[type="submit"], input[type="submit"]')
                    await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                    await asyncio.sleep(2)

                    html = await page.content()
                    return _parse_html(html, self.registry_name)
                finally:
                    await browser.close()

        except Exception as exc:
            log.warning("fssp.playwright.error", error=str(exc))
            return CheckResult(registry=self.registry_name, found=False, error=str(exc))


# ----------------------------------------------------------------- helpers

def _build_form(*, inn: str | None, name: str | None) -> dict:
    return {
        "is[region]": _REGION_ALL if inn else _REGION_OMSK,
        "is[name]": name or "",
        "is[nameType]": "1",  # юридическое лицо / ИП
        "is[inn]": inn or "",
        "is[dobDate]": "",
    }


def _has_captcha(html: str) -> bool:
    lower = html.lower()
    return "smartcaptcha" in lower or 'id="captcha"' in lower


def _parse_html(html: str, registry_name: str) -> CheckResult:
    soup = BeautifulSoup(html, "lxml")

    # FSSP result tables carry a class containing "results" or sit inside #resultList
    table = (
        soup.find("table", id="resultList")
        or soup.find("table", class_=lambda c: c and "result" in " ".join(c).lower())
        or soup.find("table")
    )

    if not table:
        page_text = soup.get_text(separator=" ").lower()
        if "не найдено" in page_text or "записей не найдено" in page_text or "ничего не найдено" in page_text:
            return CheckResult(registry=registry_name, found=False)
        return CheckResult(registry=registry_name, found=False, error="could not parse FSSP response")

    data_rows = [r for r in table.find_all("tr") if r.find("td")]
    if not data_rows:
        return CheckResult(registry=registry_name, found=False)

    items: list[dict] = []
    total_debt = 0.0

    for row in data_rows:
        cells = [td.get_text(separator=" ", strip=True) for td in row.find_all("td")]
        items.append({"cells": cells})

        # Try to pick up a monetary amount from any cell
        for cell in cells:
            cleaned = cell.replace("\u00a0", "").replace(" ", "").replace(",", ".").replace("руб.", "").strip()
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
