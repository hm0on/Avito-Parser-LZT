"""APScheduler-based weekly pipeline runner."""

import asyncio
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import structlog

from src.config import settings
from src.pipeline.runner import PipelineRunner

log = structlog.get_logger(__name__)
logging.basicConfig(level=logging.INFO)


async def run_pipeline_job() -> None:
    log.info("scheduler.job.start")
    runner = PipelineRunner()
    try:
        await runner.run()
        log.info("scheduler.job.done")
    except Exception as exc:
        log.error("scheduler.job.error", error=str(exc))


async def main() -> None:
    timezone = ZoneInfo(settings.scheduler_timezone)
    scheduler = AsyncIOScheduler(timezone=timezone)

    # Parse cron from settings (format: "minute hour day month day_of_week")
    cron_parts = settings.scheduler_cron.split()
    if len(cron_parts) == 5:
        minute, hour, day, month, day_of_week = cron_parts
    else:
        # Default: every Monday at 03:00
        minute, hour, day, month, day_of_week = "0", "3", "*", "*", "1"

    trigger = CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone=timezone,
    )

    scheduler.add_job(
        run_pipeline_job,
        trigger=trigger,
        id="weekly_pipeline",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
        replace_existing=True,
    )
    scheduler.start()
    job = scheduler.get_job("weekly_pipeline")

    log.info(
        "scheduler.started",
        cron=settings.scheduler_cron,
        timezone=settings.scheduler_timezone,
        next_run_time=job.next_run_time.isoformat() if job and job.next_run_time else None,
    )

    # Keep event loop alive
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("scheduler.stopped")


if __name__ == "__main__":
    asyncio.run(main())
