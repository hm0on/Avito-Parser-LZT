"""
Тестовый прогон полного пайплайна — все 3 коллектора, все 5 стадий,
но суммарно не более _COMPANY_LIMIT компаний.

Телефоны и отзывы запрашиваются ТОЛЬКО для итоговых
компаний (экономия кредитов).

Запуск:
    python3 run_once.py
"""
import asyncio

from src.pipeline.runner import PipelineRunner

_COMPANY_LIMIT = 3

asyncio.run(PipelineRunner(company_limit=_COMPANY_LIMIT).run())
