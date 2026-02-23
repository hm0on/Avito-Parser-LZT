"""Abstract base collector — defines the interface every collector must implement."""

import asyncio
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class RawCompany:
    """Data class for a single scraped company record."""

    source: str
    name_raw: str | None = None
    source_id: str | None = None
    source_link: str | None = None
    raw_payload: dict | None = None

    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    contacts_json: dict = field(default_factory=dict)

    inn: str | None = None
    ogrn: str | None = None

    average_rating: float | None = None
    reviews_count: int | None = None

    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Attached reviews
    reviews: list["RawReview"] = field(default_factory=list)


@dataclass
class RawReview:
    """Data class for a single scraped review."""

    source: str
    text: str | None = None
    rating: float | None = None
    author: str | None = None
    review_date: datetime | None = None
    source_link: str | None = None


class AbstractCollector(ABC):
    """Base class for all platform collectors."""

    source_name: str = ""

    @abstractmethod
    async def collect(self, keyword: str) -> list[RawCompany]:
        """Collect companies for a single keyword."""

    # Max concurrent keyword tasks per collector (override in subclass if needed)
    _max_concurrent_keywords: int = 0  # 0 = use config default
    _uses_playwright: bool = False  # set True in Playwright-based collectors

    async def run(self, keywords: list[str] | None = None) -> list[RawCompany]:
        """Run collection for all keywords concurrently and return combined results."""
        from src.config import settings

        keywords = keywords or settings.search_keywords
        if self._max_concurrent_keywords > 0:
            concurrency = self._max_concurrent_keywords
        elif self._uses_playwright:
            concurrency = settings.playwright_max_keywords
        else:
            concurrency = settings.collector_max_keywords
        sem = asyncio.Semaphore(concurrency)

        async def _collect_one(kw: str) -> list[RawCompany]:
            async with sem:
                return await self.collect(kw)

        results = await asyncio.gather(
            *[_collect_one(kw) for kw in keywords],
            return_exceptions=True,
        )

        all_results: list[RawCompany] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                import structlog
                structlog.get_logger(__name__).error(
                    "collector.keyword_error",
                    source=self.source_name,
                    keyword=keywords[i],
                    error=str(result),
                )
            else:
                all_results.extend(result)
        return all_results


# ── Shared parsing helpers ────────────────────────────────────────────────────


def parse_float(text: str) -> float | None:
    """Extract the first decimal number from text ('4,7' → 4.7)."""
    m = re.search(r"[\d]+[,.]?[\d]*", text.replace(",", "."))
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def parse_int(text: str) -> int | None:
    """Extract the first standalone integer from text ('29 отзывов' → 29).

    Matches word-boundary numbers to avoid concatenating all digits from the text.
    """
    m = re.search(r"\b(\d{1,7})\b", text)
    return int(m.group(1)) if m else None
