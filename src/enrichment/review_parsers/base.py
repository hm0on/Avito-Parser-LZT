"""Abstract base for all review platform parsers."""

from abc import ABC, abstractmethod

import structlog

from src.collectors.base import RawReview

log = structlog.get_logger(__name__)


class AbstractReviewParser(ABC):
    """Each parser handles one review platform.

    Pattern mirrors AbstractChecker.safe_check from registries/base.py.
    """

    source_name: str = ""
    supported_domains: list[str] = []

    def can_handle(self, url: str) -> bool:
        """Return True if this parser can handle the given URL."""
        url_lower = url.lower()
        return any(domain in url_lower for domain in self.supported_domains)

    @abstractmethod
    async def parse(self, url: str, company_name: str) -> list[RawReview]:
        """Fetch and parse reviews from the URL.

        Args:
            url: Page URL discovered via search.
            company_name: Human-readable name (used by VK for group lookup).

        Returns:
            List of RawReview. Empty list on failure.
        """

    async def safe_parse(self, url: str, company_name: str) -> list[RawReview]:
        """Wraps parse() — catches all exceptions, returns [] on any error."""
        try:
            return await self.parse(url, company_name)
        except Exception as exc:
            log.warning(
                "review_parser.error",
                source=self.source_name,
                url=url,
                error=str(exc),
            )
            return []
