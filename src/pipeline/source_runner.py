"""Common runner for collecting a single source into raw tables."""

from __future__ import annotations

import structlog

from src.collectors.avito_proxy_precheck import ProxyPoolExhaustedError
from src.collectors.base import AbstractCollector, RawCompany
from src.collectors.twogis import TwoGisProxyAuthError
from src.config import settings
from src.database.session import AsyncSessionLocal
from src.pipeline.common import (
    append_source_collection,
    clear_source_collection,
    current_pipeline_week_start,
    mark_checkpoint,
    reset_enrichment_checkpoint,
)
from src.relevance import RelevanceService

log = structlog.get_logger(__name__)


class SourceCollectorRunner:
    """Collect one source and persist progress batch-by-batch."""

    def __init__(
        self,
        collector: AbstractCollector,
        *,
        company_limit: int = 0,
    ) -> None:
        self.collector = collector
        self.company_limit = max(0, int(company_limit))
        self.source_name = collector.source_name
        self.relevance = RelevanceService()

    async def run(self) -> dict[str, int]:
        week_start = current_pipeline_week_start()
        await self._mark_running(week_start)

        try:
            await self._run_precheck()

            async with AsyncSessionLocal() as session:
                await clear_source_collection(
                    session,
                    source=self.source_name,
                    week_start=week_start,
                )
                await reset_enrichment_checkpoint(
                    session,
                    week_start=week_start,
                    reason=f"{self.source_name}_refreshed",
                )
                await session.commit()

            stats = await self._collect_and_persist_batches(week_start)
            if stats["companies_count"] == 0:
                raise RuntimeError(
                    f"Source {self.source_name} collected 0 companies for week {week_start}"
                )

            async with AsyncSessionLocal() as session:
                await mark_checkpoint(
                    session,
                    source=self.source_name,
                    week_start=week_start,
                    status="completed",
                    companies_count=stats["companies_count"],
                    reviews_count=stats["reviews_count"],
                    details_json={
                        "company_limit": self.company_limit or None,
                        "keywords_total": stats["keywords_total"],
                        "keywords_completed": stats["keywords_completed"],
                        "accepted_raw": stats["companies_count"],
                        "filtered_domain_out": stats["filtered_domain_out"],
                        "filtered_geo_out": stats["filtered_geo_out"],
                        "filtered_low_confidence_out": stats["filtered_low_confidence_out"],
                    },
                )
                await session.commit()

            log.info(
                "source_runner.done",
                source=self.source_name,
                week_start=str(week_start),
                companies=stats["companies_count"],
                reviews=stats["reviews_count"],
            )
            return stats
        except (TwoGisProxyAuthError, ProxyPoolExhaustedError) as exc:
            await self._mark_failed(
                week_start,
                str(exc),
                details_json={
                    "failure_type": "proxy_auth_exhausted",
                    "source": self.source_name,
                    "action_required": "Replace or fix proxies in storage/proxies/ for this source",
                },
            )
            raise
        except Exception as exc:
            await self._mark_failed(week_start, str(exc))
            raise

    async def _collect_and_persist_batches(self, week_start) -> dict[str, int]:
        keywords = list(settings.search_keywords_effective)
        total_keywords = len(keywords)
        keywords_completed = 0
        companies_total = 0
        reviews_total = 0
        filtered_domain_out = 0
        filtered_geo_out = 0
        filtered_low_confidence_out = 0

        for idx, keyword in enumerate(keywords, start=1):
            if self.company_limit and companies_total >= self.company_limit:
                log.info(
                    "source_runner.limit_reached",
                    source=self.source_name,
                    company_limit=self.company_limit,
                    companies=companies_total,
                )
                break

            remaining = 0
            if self.company_limit:
                remaining = max(0, self.company_limit - companies_total)
                if remaining == 0:
                    break

            try:
                batch = await self.collector.collect(
                    keyword,
                    max_companies=remaining,
                )
            except TypeError as exc:
                # Backward compatibility for test doubles/custom collectors
                # that still implement collect(keyword) without max_companies.
                if "max_companies" not in str(exc):
                    raise
                batch = await self.collector.collect(keyword)
            await self._run_post_collection_hooks(batch)
            batch, filter_stats = await self._apply_relevance_filter(batch, keyword=keyword)

            filtered_domain_out += filter_stats["filtered_domain_out"]
            filtered_geo_out += filter_stats["filtered_geo_out"]
            filtered_low_confidence_out += filter_stats["filtered_low_confidence_out"]

            if self.company_limit:
                batch = batch[: max(0, self.company_limit - companies_total)]

            batch_stats = {"companies_count": 0, "reviews_count": 0}
            if batch:
                async with AsyncSessionLocal() as session:
                    batch_stats = await append_source_collection(
                        session,
                        week_start=week_start,
                        raw_companies=batch,
                    )
                    companies_total += batch_stats["companies_count"]
                    reviews_total += batch_stats["reviews_count"]
                    keywords_completed = idx
                    await mark_checkpoint(
                        session,
                        source=self.source_name,
                        week_start=week_start,
                        status="running",
                        companies_count=companies_total,
                        reviews_count=reviews_total,
                        details_json={
                            "current_keyword": keyword,
                            "keywords_total": total_keywords,
                            "keywords_completed": idx,
                            "company_limit": self.company_limit or None,
                            "accepted_raw": companies_total,
                            "filtered_domain_out": filtered_domain_out,
                            "filtered_geo_out": filtered_geo_out,
                            "filtered_low_confidence_out": filtered_low_confidence_out,
                        },
                    )
                    await session.commit()
            else:
                keywords_completed = idx
                async with AsyncSessionLocal() as session:
                    await mark_checkpoint(
                        session,
                        source=self.source_name,
                        week_start=week_start,
                        status="running",
                        companies_count=companies_total,
                        reviews_count=reviews_total,
                        details_json={
                            "current_keyword": keyword,
                            "keywords_total": total_keywords,
                            "keywords_completed": idx,
                            "company_limit": self.company_limit or None,
                            "batch_empty": True,
                            "accepted_raw": companies_total,
                            "filtered_domain_out": filtered_domain_out,
                            "filtered_geo_out": filtered_geo_out,
                            "filtered_low_confidence_out": filtered_low_confidence_out,
                        },
                    )
                    await session.commit()

            log.info(
                "source_runner.batch_done",
                source=self.source_name,
                keyword=keyword,
                keyword_idx=idx,
                keywords_total=total_keywords,
                batch_companies=batch_stats["companies_count"],
                batch_reviews=batch_stats["reviews_count"],
                companies_total=companies_total,
                reviews_total=reviews_total,
            )

        return {
            "companies_count": companies_total,
            "reviews_count": reviews_total,
            "keywords_total": total_keywords,
            "keywords_completed": keywords_completed,
            "filtered_domain_out": filtered_domain_out,
            "filtered_geo_out": filtered_geo_out,
            "filtered_low_confidence_out": filtered_low_confidence_out,
        }

    async def _apply_relevance_filter(
        self,
        batch: list[RawCompany],
        *,
        keyword: str,
    ) -> tuple[list[RawCompany], dict[str, int]]:
        accepted: list[RawCompany] = []
        stats = {
            "filtered_domain_out": 0,
            "filtered_geo_out": 0,
            "filtered_low_confidence_out": 0,
        }

        for company in batch:
            decision = await self.relevance.decide(raw=company, keyword=keyword)

            payload = dict(company.raw_payload or {})
            payload["relevance"] = decision.to_dict()
            company.raw_payload = payload

            if not decision.domain_pass:
                stats["filtered_domain_out"] += 1
                continue
            if not decision.geo_pass:
                stats["filtered_geo_out"] += 1
                continue
            if decision.confidence < settings.relevance_min_confidence:
                stats["filtered_low_confidence_out"] += 1
                continue

            accepted.append(company)

        return accepted, stats

    async def _run_post_collection_hooks(self, raw_companies: list[RawCompany]) -> None:
        if settings.enable_phone_parsing and hasattr(self.collector, "enrich_phones"):
            await self.collector.enrich_phones(raw_companies)
        if hasattr(self.collector, "enrich_reviews"):
            await self.collector.enrich_reviews(raw_companies)

    async def _run_precheck(self) -> None:
        if not hasattr(self.collector, "precheck_proxies"):
            return
        try:
            summary = await self.collector.precheck_proxies()
            log.info(
                "source_runner.precheck_done",
                source=self.source_name,
                total=summary.total_tested,
                avito_ok=summary.avito_ok,
                ranked=len(summary.ranked_proxies),
            )
        except ProxyPoolExhaustedError as exc:
            log.error(
                "source_runner.precheck_failed",
                source=self.source_name,
                error=str(exc),
            )
            raise RuntimeError(
                f"Proxy pool unusable for {self.source_name}: {exc}"
            ) from exc

    async def _mark_running(self, week_start) -> None:
        async with AsyncSessionLocal() as session:
            await mark_checkpoint(
                session,
                source=self.source_name,
                week_start=week_start,
                status="running",
                companies_count=0,
                reviews_count=0,
                error_text=None,
                details_json={
                    "company_limit": self.company_limit or None,
                    "keywords_total": len(settings.search_keywords_effective),
                    "keywords_completed": 0,
                    "accepted_raw": 0,
                    "filtered_domain_out": 0,
                    "filtered_geo_out": 0,
                    "filtered_low_confidence_out": 0,
                },
            )
            await session.commit()

    async def _mark_failed(
        self,
        week_start,
        error_text: str,
        *,
        details_json: dict | None = None,
    ) -> None:
        async with AsyncSessionLocal() as session:
            await mark_checkpoint(
                session,
                source=self.source_name,
                week_start=week_start,
                status="failed",
                error_text=error_text[:1000],
                details_json=details_json,
            )
            await session.commit()
