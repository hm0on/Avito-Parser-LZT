"""
Тестовый прогон пайплайна — 1 ключевое слово, только Avito-коллектор.
(2GIS и Яндекс не работают с текущего MAC без RU-прокси для Playwright)

Запуск:
    python3 run_once.py
"""
import asyncio

# Подменяем список коллекторов ДО импорта PipelineRunner
from src.collectors.avito import AvitoCollector
from src.collectors.base import AbstractCollector

# Один keyword вместо шести
_orig_run = AbstractCollector.run

async def _one_keyword(self, keywords=None):
    return await _orig_run(self, keywords=["бурение скважин"])

AbstractCollector.run = _one_keyword

# Патчим PipelineRunner чтобы использовал только Avito
import src.pipeline.runner as _runner_module

_OrigRunner = _runner_module.PipelineRunner

class _AvitoOnlyRunner(_OrigRunner):
    def __init__(self):
        super().__init__()
        # Оставляем только Avito (2GIS и Яндекс таймаутятся без RU-прокси)
        self.collectors = [AvitoCollector()]

_runner_module.PipelineRunner = _AvitoOnlyRunner

from src.pipeline.runner import PipelineRunner
asyncio.run(PipelineRunner().run())
