"""Weekly orchestrator that delegates work to source-specific runners."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from src.config import settings
from src.pipeline.avito_runner import AvitoRunner
from src.pipeline.enrichment_runner import EnrichmentRunner
from src.pipeline.twogis_runner import TwoGisRunner
from src.pipeline.yandex_runner import YandexRunner

log = structlog.get_logger(__name__)


class PipelineRunner:
    """Backward-compatible entrypoint for the weekly multi-runner workflow."""

    def __init__(self, company_limit: int = 0) -> None:
        self.company_limit = max(0, int(company_limit))
        self.per_source_limit = max(1, self.company_limit // 3) if self.company_limit else 0
        self.avito_runner = AvitoRunner(company_limit=self.per_source_limit)
        self.twogis_runner = TwoGisRunner(company_limit=self.per_source_limit)
        self.yandex_runner = YandexRunner(company_limit=self.per_source_limit)
        self.enrichment_runner = EnrichmentRunner()

    async def run(self) -> None:
        log.info(
            "pipeline.start",
            company_limit=self.company_limit or None,
            per_source_limit=self.per_source_limit or None,
            parallel_sources=settings.pipeline_parallel_sources,
            source_parallelism=max(1, settings.pipeline_source_parallelism),
        )
        start = datetime.now(UTC)

        await self._run_sources()
        enrichment_started = await self.enrichment_runner.run()

        elapsed = (datetime.now(UTC) - start).total_seconds()
        log.info("pipeline.done", elapsed_s=elapsed, enrichment_started=enrichment_started)

    async def _run_sources(self) -> None:
        source_runners = [
            ("avito", self.avito_runner),
            ("twogis", self.twogis_runner),
            ("yandex", self.yandex_runner),
        ]

        if not settings.pipeline_parallel_sources:
            for name, runner in source_runners:
                log.info("pipeline.source.start", source=name, mode="sequential")
                await runner.run()
                log.info("pipeline.source.done", source=name, mode="sequential")
            return

        sem = asyncio.Semaphore(max(1, settings.pipeline_source_parallelism))

        async def _run_one(name: str, runner) -> tuple[str, Exception | None]:
            async with sem:
                log.info("pipeline.source.start", source=name, mode="parallel")
                try:
                    await runner.run()
                    log.info("pipeline.source.done", source=name, mode="parallel")
                    return name, None
                except Exception as exc:
                    log.error("pipeline.source.failed", source=name, error=str(exc))
                    return name, exc

        results = await asyncio.gather(*[_run_one(name, runner) for name, runner in source_runners])
        errors = [(name, exc) for name, exc in results if exc is not None]
        if errors:
            joined = "; ".join(f"{name}: {err}" for name, err in errors)
            raise RuntimeError(f"One or more source runners failed: {joined}")


async def main() -> None:
    await PipelineRunner().run()


if __name__ == "__main__":
    asyncio.run(main())
