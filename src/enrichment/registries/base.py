"""Abstract registry checker interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class CheckResult:
    """Result returned by each registry checker."""

    registry: str
    found: bool = False
    status: str | None = None  # e.g. 'active', 'liquidated', 'bankrupt'
    details: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "registry": self.registry,
            "found": self.found,
            "status": self.status,
            "details": self.details,
            "error": self.error,
        }


class AbstractChecker(ABC):
    """Base class for all registry checkers."""

    registry_name: str = ""

    @abstractmethod
    async def check(self, inn: str | None = None, ogrn: str | None = None, name: str | None = None) -> CheckResult:
        """Perform registry check and return result."""

    async def safe_check(
        self,
        inn: str | None = None,
        ogrn: str | None = None,
        name: str | None = None,
    ) -> CheckResult:
        """Wrapper that catches all exceptions and returns error result."""
        try:
            return await self.check(inn=inn, ogrn=ogrn, name=name)
        except Exception as exc:
            return CheckResult(
                registry=self.registry_name,
                found=False,
                error=str(exc),
            )
