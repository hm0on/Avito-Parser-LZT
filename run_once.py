"""
Тестовый прогон полного пайплайна — все 3 коллектора, все 5 стадий,
но суммарно не более _COMPANY_LIMIT компаний.

Лимит распределяется равномерно по источникам (avito, 2gis, yandex),
чтобы 2GIS-компании (с отзывами) попадали в выборку.

Запуск:
    python3 run_once.py
"""
import asyncio
import logging

import structlog

from src.pipeline.runner import PipelineRunner

_COMPANY_LIMIT = 30  # 2 per source (avito, 2gis, yandex)

# Show all pipeline logs (including debug for review diagnostics)
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
)

asyncio.run(PipelineRunner(company_limit=_COMPANY_LIMIT).run())
