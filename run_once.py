"""Local one-shot run of the weekly multi-runner pipeline."""

import asyncio
import logging

import structlog

from src.pipeline.runner import PipelineRunner

_COMPANY_LIMIT = 6  # total limit, distributed across avito / 2gis / yandex

# Show all pipeline logs (including debug for review diagnostics)
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
)

asyncio.run(PipelineRunner(company_limit=_COMPANY_LIMIT).run())
