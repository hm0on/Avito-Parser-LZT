"""Weekly enrichment runner that starts only after all source collectors finish."""

from __future__ import annotations

import asyncio
import uuid

import structlog

from src.company_identity import build_identity_key
from src.ai.risk_assessor import RiskAssessor
from src.collectors.base import RawReview
from src.config import settings
from src.database.models import CompanyEnriched, CompanyRaw, ReviewRaw
from src.database.session import AsyncSessionLocal
from src.deduplication.deduplicator import Deduplicator
from src.enrichment.enricher import Enricher
from src.enrichment.legal_bindings import load_legal_bindings, upsert_legal_binding
from src.pipeline.clean_builder import rebuild_clean_rows_for_week
from src.pipeline.common import (
    cleanup_week_enrichment_outputs,
    current_pipeline_week_start,
    load_week_raw_records,
    mark_checkpoint,
    required_sources_completed,
)

log = structlog.get_logger(__name__)


class EnrichmentRunner:
    """Runs enrichment/dedup/AI for the current weekly raw snapshot."""

    def __init__(self) -> None:
        self.enricher = Enricher()
        self.deduplicator = Deduplicator()
        self.risk_assessor = RiskAssessor()

    async def run(self) -> bool:
        week_start = current_pipeline_week_start()

        async with AsyncSessionLocal() as session:
            if not await required_sources_completed(session, week_start=week_start):
                await mark_checkpoint(
                    session,
                    source="enrichment",
                    week_start=week_start,
                    status="pending",
                    details_json={"reason": "waiting_for_collection_sources"},
                )
                await session.commit()
                return False

        await self._mark_running(week_start)

        try:
            async with AsyncSessionLocal() as session:
                await cleanup_week_enrichment_outputs(session, week_start=week_start)
                db_raws = await load_week_raw_records(session, week_start=week_start)
                if not db_raws:
                    await mark_checkpoint(
                        session,
                        source="enrichment",
                        week_start=week_start,
                        status="pending",
                        details_json={"reason": "no_raw_data_for_week"},
                    )
                    await session.commit()
                    return False

                db_id_map = {str(db_raw.id): db_raw for db_raw in db_raws}
                enriched_pairs, extra_reviews_map = await self._enrich_rows(
                    session,
                    db_raws,
                    week_start=week_start,
                )
                if not enriched_pairs:
                    raise RuntimeError("Enrichment produced no records for the current week")

                legal_verified_pairs = [pair for pair in enriched_pairs if pair[1].legal_verified]
                rejected_unverified_total = len(enriched_pairs) - len(legal_verified_pairs)

                clean_result = await rebuild_clean_rows_for_week(
                    session,
                    week_start=week_start,
                    deduplicator=self.deduplicator,
                    risk_assessor=self.risk_assessor,
                )
                total_reviews = clean_result.total_reviews

                await mark_checkpoint(
                    session,
                    source="enrichment",
                    week_start=week_start,
                    status="completed",
                    companies_count=len(clean_result.rows),
                    reviews_count=total_reviews,
                    details_json={
                        "raw_total": len(db_raws),
                        "domain_geo_passed": len(db_raws),
                        "enriched_pairs": len(enriched_pairs),
                        "legal_verified_total": len(legal_verified_pairs),
                        "clean_written_total": len(clean_result.rows),
                        "rejected_unverified_total": rejected_unverified_total,
                        },
                )
                await session.commit()

            log.info(
                "enrichment_runner.done",
                week_start=str(week_start),
                raw_companies=len(db_raws),
                canonical_cards=len(clean_result.rows),
                total_reviews=total_reviews,
            )
            return True
        except Exception as exc:
            await self._mark_failed(week_start, str(exc))
            raise

    async def _enrich_rows(
        self,
        session,
        db_raws: list[CompanyRaw],
        *,
        week_start,
    ) -> tuple[list[tuple[CompanyRaw, CompanyEnriched]], dict[str, list[RawReview]]]:
        enriched_pairs: list[tuple[CompanyRaw, CompanyEnriched]] = []
        extra_reviews_map: dict[str, list[RawReview]] = {}
        enrich_sem = asyncio.Semaphore(max(1, settings.enrichment_workers))
        identity_keys = [build_identity_key(db_raw) for db_raw in db_raws]
        bindings_by_identity = await load_legal_bindings(session, identity_keys=identity_keys)

        async def _enrich_one(db_raw: CompanyRaw):
            async with enrich_sem:
                return db_raw, await self.enricher.enrich(
                    db_raw,
                    existing_binding=bindings_by_identity.get(build_identity_key(db_raw)),
                )

        enrich_results = await asyncio.gather(
            *[_enrich_one(db_raw) for db_raw in db_raws],
            return_exceptions=True,
        )

        for result in enrich_results:
            if isinstance(result, Exception):
                log.error("enrichment_runner.enrich.error", error=str(result))
                continue

            db_raw, (enriched, extra_reviews) = result
            enriched.pipeline_week_start = week_start
            session.add(enriched)
            enriched_pairs.append((db_raw, enriched))
            db_raw.is_processed = True
            legal_match = enriched.checks.get("legal_match", {}) if isinstance(enriched.checks, dict) else {}
            if legal_match.get("verified") and legal_match.get("bindable"):
                await upsert_legal_binding(
                    session,
                    raw=db_raw,
                    decision=self._decision_from_legal_match(enriched),
                )

            if extra_reviews:
                extra_reviews_map[str(db_raw.id)] = extra_reviews
                for review in extra_reviews:
                    session.add(
                        ReviewRaw(
                            id=uuid.uuid4(),
                            company_raw_id=db_raw.id,
                            source=review.source,
                            text=review.text,
                            rating=review.rating,
                            author=review.author,
                            review_date=review.review_date,
                            source_link=review.source_link,
                        )
                    )

        await session.flush()
        return enriched_pairs, extra_reviews_map

    def _decision_from_legal_match(self, enriched: CompanyEnriched):
        from src.enrichment.legal_bindings import LegalMatchDecision

        legal_match = enriched.checks.get("legal_match", {}) if isinstance(enriched.checks, dict) else {}
        evidence = legal_match.get("evidence", {}) if isinstance(legal_match, dict) else {}
        return LegalMatchDecision(
            inn=enriched.inn,
            ogrn=enriched.ogrn,
            entity_type=enriched.entity_type or "unknown",
            legal_name=evidence.get("legal_name"),
            legal_verified=bool(legal_match.get("verified")),
            legal_match_method=enriched.legal_match_method,
            legal_match_score=enriched.legal_match_score,
            evidence=evidence if isinstance(evidence, dict) else {},
            bindable=bool(legal_match.get("bindable")),
            binding_strength=legal_match.get("binding_strength"),
            binding_source=legal_match.get("binding_source"),
            conflict=bool(legal_match.get("conflict")),
        )

    async def _mark_running(self, week_start) -> None:
        async with AsyncSessionLocal() as session:
            await mark_checkpoint(
                session,
                source="enrichment",
                week_start=week_start,
                status="running",
                companies_count=0,
                reviews_count=0,
                error_text=None,
            )
            await session.commit()

    async def _mark_failed(self, week_start, error_text: str) -> None:
        async with AsyncSessionLocal() as session:
            await mark_checkpoint(
                session,
                source="enrichment",
                week_start=week_start,
                status="failed",
                error_text=error_text[:1000],
            )
            await session.commit()


async def main() -> None:
    await EnrichmentRunner().run()


if __name__ == "__main__":
    asyncio.run(main())
