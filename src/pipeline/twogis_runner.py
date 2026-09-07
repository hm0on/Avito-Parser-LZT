"""Weekly raw collector runner for 2GIS."""

from __future__ import annotations

import asyncio

from src.collectors.twogis import TwoGisCollector
from src.pipeline.source_runner import SourceCollectorRunner


class TwoGisRunner(SourceCollectorRunner):
    def __init__(self, *, company_limit: int = 0) -> None:
        super().__init__(TwoGisCollector(), company_limit=company_limit)


async def main() -> None:
    await TwoGisRunner().run()


if __name__ == "__main__":
    asyncio.run(main())
