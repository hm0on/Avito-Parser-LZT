"""Abstract base collector — defines the interface every collector must implement."""

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
    async def collect(self, keyword: str, *, max_companies: int = 0) -> list[RawCompany]:
        """Collect companies for a single keyword."""

    async def run(self, keywords: list[str] | None = None) -> list[RawCompany]:
        """Run collection for all keywords and return combined results."""
        from src.config import settings

        keywords = keywords or settings.search_keywords_effective
        all_results: list[RawCompany] = []
        for kw in keywords:
            results = await self.collect(kw)
            all_results.extend(results)
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
    """Extract the first integer from text ('29 отзывов' → 29)."""
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None
