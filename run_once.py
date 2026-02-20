"""
Тестовый прогон полного пайплайна — все 3 коллектора, все 5 стадий,
но суммарно не более _COMPANY_LIMIT компаний.

Телефоны запрашиваются через spfa.ru/api/phone/ ТОЛЬКО для итоговых
компаний (экономия кредитов).

Запуск:
    python3 run_once.py
"""
import asyncio

import structlog

from src.collectors.base import RawCompany
import src.pipeline.runner as _runner_module

log = structlog.get_logger("run_once")

_COMPANY_LIMIT = 5  # сколько компаний передать дальше по пайплайну

_OrigRunner = _runner_module.PipelineRunner


class _LimitedRunner(_OrigRunner):
    """Полный пайплайн, но ограничивает количество компаний.

    Телефоны запрашиваются после обрезки, чтобы не тратить кредиты впустую.
    """

    async def _collect_all(self) -> list[RawCompany]:
        # Собираем компании БЕЗ телефонов (enrich_phones вызывается в super)
        # Переопределяем чтобы: собрать → обрезать → телефоны
        tasks = [collector.run() for collector in self.collectors]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        all_companies: list[RawCompany] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                log.error(
                    "pipeline.collector.error",
                    collector=self.collectors[i].source_name,
                    error=str(result),
                )
            else:
                all_companies.extend(result)

        # Обрезаем до лимита ПЕРЕД запросом телефонов
        if len(all_companies) > _COMPANY_LIMIT:
            log.info(
                "run_once.limit_applied",
                original=len(all_companies),
                limited=_COMPANY_LIMIT,
            )
            all_companies = all_companies[:_COMPANY_LIMIT]

        # Теперь запрашиваем телефоны только для отобранных компаний
        for collector in self.collectors:
            if hasattr(collector, "enrich_phones"):
                await collector.enrich_phones(all_companies)

        return all_companies


_runner_module.PipelineRunner = _LimitedRunner

from src.pipeline.runner import PipelineRunner  # noqa: E402

asyncio.run(PipelineRunner().run())
