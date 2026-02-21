"""Full pipeline runner — 5 stages.

Usage:
    python -m src.pipeline.runner
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import structlog

from src.ai.risk_assessor import RiskAssessor
from src.ai.summarizer import ReviewSummarizer
from src.collectors.avito import AvitoCollector
from src.collectors.base import RawCompany, RawReview
from src.collectors.twogis import TwoGisCollector
from src.collectors.yandex import YandexCollector
from src.config import settings
from src.database.models import CompanyClean, CompanyEnriched, CompanyRaw, ReviewRaw
from src.database.session import AsyncSessionLocal
from src.deduplication.deduplicator import CanonicalCard, Deduplicator
from src.enrichment.enricher import Enricher

log = structlog.get_logger(__name__)


class PipelineRunner:
    """Orchestrates all 5 pipeline stages."""

    def __init__(self, company_limit: int = 0) -> None:
        self.collectors = [
            AvitoCollector(),
            TwoGisCollector(),
            YandexCollector(),
        ]
        self.company_limit = company_limit
        self.enricher = Enricher()
        self.deduplicator = Deduplicator()
        self.summarizer = ReviewSummarizer()
        self.risk_assessor = RiskAssessor()

    async def run(self) -> None:
        log.info("pipeline.start")
        start = datetime.now(UTC)

        async with AsyncSessionLocal() as session:
            # ── Stage 1: Collect ─────────────────────────────────────────
            log.info("pipeline.stage1.collect")
            raw_companies = await self._collect_all()
            log.info("pipeline.stage1.done", total=len(raw_companies))

            # Persist raw records and build db_id_map for later review lookup
            db_raws: list[CompanyRaw] = []
            db_id_map: dict[str, RawCompany] = {}  # str(db_raw.id) → RawCompany

            for rc in raw_companies:
                db_raw = _raw_company_to_orm(rc)
                session.add(db_raw)
                db_raws.append(db_raw)
                db_id_map[str(db_raw.id)] = rc

                for rv in rc.reviews:
                    session.add(ReviewRaw(
                        id=uuid.uuid4(),
                        company_raw_id=db_raw.id,
                        source=rv.source,
                        text=rv.text,
                        rating=rv.rating,
                        author=rv.author,
                        review_date=rv.review_date,
                        source_link=rv.source_link,
                    ))

            await session.flush()
            log.info("pipeline.stage1.persisted", count=len(db_raws))

            # ── Stage 2: Enrich (concurrent) ─────────────────────────────
            log.info("pipeline.stage2.enrich")
            enriched_pairs: list[tuple[CompanyRaw, CompanyEnriched]] = []
            # Map raw_id → extra reviews found by ReviewSearcher (Flamp/VK/Otzovik)
            extra_reviews_map: dict[str, list[RawReview]] = {}

            enrich_sem = asyncio.Semaphore(3)

            async def _enrich_one(db_raw: CompanyRaw):
                async with enrich_sem:
                    return db_raw, await self.enricher.enrich(db_raw)

            enrich_results = await asyncio.gather(
                *[_enrich_one(db_raw) for db_raw in db_raws],
                return_exceptions=True,
            )

            for result in enrich_results:
                if isinstance(result, Exception):
                    log.error("pipeline.enrich.error", error=str(result))
                    continue
                db_raw, (enriched, extra_reviews) = result
                session.add(enriched)
                enriched_pairs.append((db_raw, enriched))
                db_raw.is_processed = True

                # Persist enrichment reviews and cache them for AI stage
                if extra_reviews:
                    extra_reviews_map[str(db_raw.id)] = extra_reviews
                    for rv in extra_reviews:
                        session.add(ReviewRaw(
                            id=uuid.uuid4(),
                            company_raw_id=db_raw.id,
                            source=rv.source,
                            text=rv.text,
                            rating=rv.rating,
                            author=rv.author,
                            review_date=rv.review_date,
                            source_link=rv.source_link,
                        ))

            await session.flush()
            log.info(
                "pipeline.stage2.done",
                count=len(enriched_pairs),
                extra_reviews_total=sum(len(v) for v in extra_reviews_map.values()),
                companies_with_extra_reviews=len(extra_reviews_map),
            )

            # ── Stage 3: Deduplicate ─────────────────────────────────────
            log.info("pipeline.stage3.dedup")
            canonical_cards = self.deduplicator.run(enriched_pairs)
            log.info("pipeline.stage3.done", canonical_count=len(canonical_cards))

            # ── Stage 4 + 5: AI summarize, risk assess, write clean ──────
            log.info("pipeline.stage4.ai_and_write")
            ai_sem = asyncio.Semaphore(5)

            async def _process_card(card: CanonicalCard):
                async with ai_sem:
                    # Gather reviews from Stage 1 collectors + Stage 2 enrichment
                    reviews_for_ai = _gather_reviews(card, db_id_map, extra_reviews_map)

                    # Update reviews_count / average_rating from actually collected reviews
                    # Use the larger of metadata count vs actual collected texts
                    if reviews_for_ai:
                        card.reviews_count = max(card.reviews_count or 0, len(reviews_for_ai))
                        ratings = [r["rating"] for r in reviews_for_ai if r.get("rating")]
                        if ratings:
                            card.average_rating = round(sum(ratings) / len(ratings), 2)

                    summary = await self.summarizer.summarize(
                        company_name=card.name_normalized,
                        reviews=reviews_for_ai,
                    )
                    card.reviews_sample = reviews_for_ai[: settings.max_reviews_per_company]

                    risk_level, risk_reasons = await self.risk_assessor.assess(
                        checks=card.checks,
                        reviews=reviews_for_ai,
                        company_name=card.name_normalized,
                    )
                    return card, summary, risk_level, risk_reasons

            ai_results = await asyncio.gather(
                *[_process_card(card) for card in canonical_cards],
                return_exceptions=True,
            )
            for result in ai_results:
                if isinstance(result, Exception):
                    log.error("pipeline.ai.error", error=str(result))
                    continue
                card, summary, risk_level, risk_reasons = result
                clean = _canonical_to_orm(card, summary, risk_level, risk_reasons)
                session.add(clean)

            await session.commit()
            elapsed = (datetime.now(UTC) - start).total_seconds()
            log.info("pipeline.done", elapsed_s=elapsed, canonical_count=len(canonical_cards))

    async def _collect_all(self) -> list[RawCompany]:
        tasks = [collector.run() for collector in self.collectors]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Group by source so we can distribute the limit evenly
        by_source: dict[str, list[RawCompany]] = {}
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                log.error(
                    "pipeline.collector.error",
                    collector=self.collectors[i].source_name,
                    error=str(result),
                )
            else:
                source = self.collectors[i].source_name
                by_source[source] = result

        log.info(
            "pipeline.collected_per_source",
            **{src: len(items) for src, items in by_source.items()},
        )

        # Apply limit evenly across sources (round-robin)
        all_companies: list[RawCompany] = []
        if self.company_limit and sum(len(v) for v in by_source.values()) > self.company_limit:
            per_source = max(1, self.company_limit // max(len(by_source), 1))
            for src, items in by_source.items():
                all_companies.extend(items[:per_source])
            # Fill remaining slots from sources that have more
            remaining = self.company_limit - len(all_companies)
            if remaining > 0:
                used = set(id(c) for c in all_companies)
                for items in by_source.values():
                    for c in items:
                        if id(c) not in used:
                            all_companies.append(c)
                            remaining -= 1
                            if remaining <= 0:
                                break
                    if remaining <= 0:
                        break
            log.info(
                "pipeline.limit_applied",
                original=sum(len(v) for v in by_source.values()),
                limited=len(all_companies),
            )
        else:
            for items in by_source.values():
                all_companies.extend(items)

        # Log review counts BEFORE enrichment
        reviews_before = sum(len(c.reviews) for c in all_companies)
        log.info(
            "pipeline.pre_enrich",
            companies=len(all_companies),
            companies_with_reviews=sum(1 for c in all_companies if c.reviews),
            total_reviews=reviews_before,
        )

        # Post-collection enrichment (phones, reviews)
        for collector in self.collectors:
            if hasattr(collector, "enrich_phones"):
                await collector.enrich_phones(all_companies)
            if hasattr(collector, "enrich_reviews"):
                await collector.enrich_reviews(all_companies)

        # Log review counts AFTER enrichment
        reviews_after = sum(len(c.reviews) for c in all_companies)
        log.info(
            "pipeline.post_enrich",
            companies_with_reviews=sum(1 for c in all_companies if c.reviews),
            total_reviews=reviews_after,
        )

        return all_companies


# ── Helpers ───────────────────────────────────────────────────────────────────


def _raw_company_to_orm(rc: RawCompany) -> CompanyRaw:
    return CompanyRaw(
        id=uuid.uuid4(),
        source=rc.source,
        source_id=rc.source_id,
        source_link=rc.source_link,
        raw_payload=rc.raw_payload,
        name_raw=rc.name_raw,
        phones=rc.phones or [],
        emails=rc.emails or [],
        addresses=rc.addresses or [],
        contacts_json=rc.contacts_json or {},
        inn=rc.inn,
        ogrn=rc.ogrn,
        average_rating=rc.average_rating,
        reviews_count=rc.reviews_count,
        collected_at=rc.collected_at,
        is_processed=False,
    )


def _rv_to_dict(rv: RawReview) -> dict:
    return {
        "text": rv.text,
        "rating": rv.rating,
        "author": rv.author,
        "review_date": rv.review_date.isoformat() if rv.review_date else None,
        "source_link": rv.source_link,
        "source": rv.source,
    }


def _gather_reviews(
    card: CanonicalCard,
    db_id_map: dict[str, RawCompany],
    extra_reviews_map: dict[str, list[RawReview]],
) -> list[dict]:
    """Collect reviews for this canonical card from all sources.

    - Stage-1 reviews: from the original collector (Avito/2GIS/Yandex)
    - Stage-2 reviews: from ReviewSearcher (Flamp/VK/Otzovik)

    Filters strictly to raw_ids belonging to this canonical card.
    """
    source_ids = set(card.source_records)  # str UUIDs of CompanyRaw rows
    reviews: list[dict] = []
    stage1_count = 0
    stage2_count = 0

    for raw_id in source_ids:
        # Stage-1 collector reviews
        rc = db_id_map.get(raw_id)
        if rc:
            for rv in rc.reviews:
                reviews.append(_rv_to_dict(rv))
                stage1_count += 1

        # Stage-2 enrichment reviews (Flamp/VK/Otzovik)
        for rv in extra_reviews_map.get(raw_id, []):
            reviews.append(_rv_to_dict(rv))
            stage2_count += 1

    log.debug(
        "gather_reviews",
        company=card.name_normalized,
        source_ids=len(source_ids),
        stage1_reviews=stage1_count,
        stage2_reviews=stage2_count,
        total=len(reviews),
    )

    return reviews[: settings.max_reviews_per_company]


def _canonical_to_orm(
    card: CanonicalCard,
    summary: str | None,
    risk_level: str,
    risk_reasons: list[str],
) -> CompanyClean:
    return CompanyClean(
        id=uuid.uuid4(),
        inn=card.inn,
        ogrn=card.ogrn,
        name_normalized=card.name_normalized,
        entity_type=card.entity_type,
        phones=card.phones,
        emails=card.emails,
        addresses=card.addresses,
        contacts_json=card.contacts_json,
        average_rating=card.average_rating,
        reviews_count=card.reviews_count,
        reviews_sample=card.reviews_sample,
        summary_review=summary,
        risk_level=risk_level,
        risk_reasons=risk_reasons,
        source_records=card.source_records,
        merged_sources=card.merged_sources,
        checks=card.checks,
        manual_review_required=card.manual_review_required,
    )


async def main() -> None:
    runner = PipelineRunner()
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
