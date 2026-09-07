"""Weekly raw collector runner for Yandex."""

from __future__ import annotations

import asyncio

from src.collectors.yandex import YandexCollector
from src.pipeline.source_runner import SourceCollectorRunner


class YandexRunner(SourceCollectorRunner):
    def __init__(self, *, company_limit: int = 0) -> None:
        super().__init__(YandexCollector(), company_limit=company_limit)


async def main() -> None:
    await YandexRunner().run()


if __name__ == "__main__":
    asyncio.run(main())
