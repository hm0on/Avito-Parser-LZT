"""Weekly raw collector runner for Avito."""

from __future__ import annotations

import asyncio

from src.collectors.avito import AvitoCollector
from src.pipeline.source_runner import SourceCollectorRunner


class AvitoRunner(SourceCollectorRunner):
    def __init__(self, *, company_limit: int = 0) -> None:
        super().__init__(AvitoCollector(), company_limit=company_limit)


async def main() -> None:
    await AvitoRunner().run()


if __name__ == "__main__":
    asyncio.run(main())
