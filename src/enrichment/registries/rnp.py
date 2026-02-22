"""РНП — реестр недобросовестных поставщиков (zakupki.gov.ru).

КРИТИЧЕСКИЙ красный флаг: попадание в РНП = серьёзный негативный сигнал.
"""

import httpx
import structlog
from bs4 import BeautifulSoup

from src.enrichment.registries.base import AbstractChecker, CheckResult

log = structlog.get_logger(__name__)

_SEARCH_URL = "https://zakupki.gov.ru/epz/dishonestsupplier/search/results.html"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


class RnpChecker(AbstractChecker):
    registry_name = "rnp"

    async def check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
        extra: dict | None = None,
    ) -> CheckResult:
        query = inn or ogrn
        if not query:
            return CheckResult(registry=self.registry_name, found=False, error="INN or OGRN required")

        params = {
            "searchString": query,
            "morphology": "on",
            "search-filter": "Дата+обновления",
            "sortDirection": "false",
            "recordsPerPage": "_10",
            "showLotsInfoHidden": "false",
            "fz44": "on",
            "fz223": "on",
            "ppRf615": "on",
            "af": "on",
            "pageNumber": "1",
        }

        async with httpx.AsyncClient(timeout=20.0, headers=_HEADERS, follow_redirects=True) as client:
            resp = await client.get(_SEARCH_URL, params=params)

            if resp.status_code in (403, 429):
                return CheckResult(registry=self.registry_name, found=False, error=f"HTTP {resp.status_code}")

            resp.raise_for_status()
            html = resp.text

        return self._parse_html(html)

    def _parse_html(self, html: str) -> CheckResult:
        soup = BeautifulSoup(html, "lxml")

        # Check for "no results" indicators
        no_results = soup.find(string=lambda t: t and ("не найдено" in t.lower() or "ничего не найдено" in t.lower()))
        total_el = soup.find("div", class_="search-results__total")
        if total_el:
            total_text = total_el.get_text(strip=True)
            if "0" in total_text or "не найдено" in total_text.lower():
                return CheckResult(registry=self.registry_name, found=False)

        # Look for result rows
        rows = soup.find_all("div", class_="registry-entry__form") or soup.find_all("div", class_="search-registry-entry-block")
        if not rows:
            # Try table-based layout
            rows = soup.find_all("tr", class_=lambda c: c and "registry" in " ".join(c).lower()) if not no_results else []

        if not rows and no_results:
            return CheckResult(registry=self.registry_name, found=False)

        if not rows:
            # Can't determine — page structure may have changed
            # Check if the page contains any INN-like match in the body
            body_text = soup.get_text()
            if "реестровый номер" in body_text.lower() or "недобросовестн" in body_text.lower():
                log.warning("rnp.parse_uncertain", hint="found keywords but no structured rows")
                return CheckResult(
                    registry=self.registry_name,
                    found=True,
                    status="in_rnp",
                    details={"in_rnp": True, "records_count": 0, "records": [], "parse_note": "structured parsing failed"},
                )
            return CheckResult(registry=self.registry_name, found=False)

        records: list[dict] = []
        for row in rows[:5]:
            text = row.get_text(separator=" ", strip=True)
            record: dict = {"raw_text": text[:300]}

            # Try to extract registry number
            num_el = row.find("div", class_="registry-entry__header-mid__number") or row.find(class_=lambda c: c and "number" in str(c))
            if num_el:
                record["registry_number"] = num_el.get_text(strip=True)

            records.append(record)

        log.info("rnp.found", records_count=len(records))

        return CheckResult(
            registry=self.registry_name,
            found=True,
            status="in_rnp",
            details={
                "in_rnp": True,
                "records_count": len(records),
                "records": records,
            },
        )
