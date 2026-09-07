"""Build final clean rows from enriched data, with manual merge and summary overrides."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.ai.risk_assessor import RiskAssessor
from src.company_identity import (
    MERGE_IDENTITY_PREFIX,
    build_identity_key,
    build_merge_identity_key,
    clean_uuid_for_identity,
)
from src.collectors.base import RawReview
from src.config import settings
from src.database.models import (
    CompanyClean,
    CompanyEnriched,
    CompanyMergeGroup,
    CompanyMergeMember,
    CompanyRaw,
    CompanySummaryOverride,
)
from src.deduplication.deduplicator import CanonicalCard, Deduplicator
from src.pipeline.common import gather_reviews

log = structlog.get_logger(__name__)


@dataclass
class CleanRebuildResult:
    rows: list[CompanyClean]
    total_reviews: int
    source_total: int


@dataclass
class MergeState:
    groups: dict[uuid.UUID, CompanyMergeGroup]
    members_by_group: dict[uuid.UUID, list[str]]
    group_by_identity: dict[str, uuid.UUID]


async def load_week_enriched_pairs(
    session: AsyncSession,
    *,
    week_start: date,
) -> list[tuple[CompanyRaw, CompanyEnriched]]:
    stmt = (
        select(CompanyRaw, CompanyEnriched)
        .join(CompanyEnriched, CompanyEnriched.raw_id == CompanyRaw.id)
        .options(selectinload(CompanyRaw.reviews))
        .where(CompanyEnriched.pipeline_week_start == week_start)
        .order_by(CompanyRaw.collected_at.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


async def load_merge_state(session: AsyncSession) -> MergeState:
    groups = list((await session.execute(select(CompanyMergeGroup))).scalars().all())
    members = list((await session.execute(select(CompanyMergeMember))).scalars().all())

    groups_by_id = {group.id: group for group in groups}
    members_by_group: dict[uuid.UUID, list[str]] = defaultdict(list)
    group_by_identity: dict[str, uuid.UUID] = {}
    for member in members:
        members_by_group[member.group_id].append(member.identity_key)
        group_by_identity[member.identity_key] = member.group_id

    return MergeState(
        groups=groups_by_id,
        members_by_group=dict(members_by_group),
        group_by_identity=group_by_identity,
    )


async def load_summary_overrides(session: AsyncSession) -> dict[str, str]:
    rows = list((await session.execute(select(CompanySummaryOverride))).scalars().all())
    return {row.identity_key: row.summary_review for row in rows}


async def upsert_summary_override(
    session: AsyncSession,
    *,
    identity_key: str,
    summary_review: str,
) -> CompanySummaryOverride:
    stmt = select(CompanySummaryOverride).where(CompanySummaryOverride.identity_key == identity_key)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = CompanySummaryOverride(identity_key=identity_key, summary_review=summary_review)
        session.add(row)
    else:
        row.summary_review = summary_review
    await session.flush()
    return row


def _build_identity_maps(
    enriched_pairs: list[tuple[CompanyRaw, CompanyEnriched]],
    deduplicator: Deduplicator,
) -> tuple[
    dict[str, tuple[CompanyRaw, CompanyEnriched]],
    dict[str, CanonicalCard],
    dict[str, set[str]],
]:
    pair_by_identity: dict[str, tuple[CompanyRaw, CompanyEnriched]] = {}
    card_by_identity: dict[str, CanonicalCard] = {}

    for raw, enriched in enriched_pairs:
        identity_key = build_identity_key(raw)
        pair_by_identity[identity_key] = (raw, enriched)
        card_by_identity[identity_key] = deduplicator.build_single_card(raw, enriched, identity_key=identity_key)

    peer_map: dict[str, set[str]] = defaultdict(set)
    for group in deduplicator.detect_groups(enriched_pairs):
        if len(group) <= 1:
            continue
        identities = [build_identity_key(raw) for raw, _ in group]
        for identity in identities:
            peer_map[identity].update(other for other in identities if other != identity)

    return pair_by_identity, card_by_identity, dict(peer_map)


def _visible_cards_with_manual_merges(
    pair_by_identity: dict[str, tuple[CompanyRaw, CompanyEnriched]],
    card_by_identity: dict[str, CanonicalCard],
    merge_state: MergeState,
    deduplicator: Deduplicator,
) -> tuple[list[CanonicalCard], dict[str, uuid.UUID]]:
    visible_cards: list[CanonicalCard] = []
    identity_to_visible_company_id: dict[str, uuid.UUID] = {}
    hidden_identities: set[str] = set()

    for group_id, group in merge_state.groups.items():
        present_identities = [
            identity
            for identity in merge_state.members_by_group.get(group_id, [])
            if identity in pair_by_identity
        ]
        if len(present_identities) < 2:
            continue

        group_pairs = [pair_by_identity[identity] for identity in present_identities]
        merged = deduplicator.merge_group(
            group_pairs,
            primary_identity_key=group.primary_identity_key,
            identity_key_getter=lambda raw, _: build_identity_key(raw),
        )
        merged.identity_key = build_merge_identity_key(group.id)
        merged.merge_group_id = str(group.id)
        merged.similar_company_ids = []
        visible_cards.append(merged)

        for identity in present_identities:
            hidden_identities.add(identity)
            identity_to_visible_company_id[identity] = group.master_company_id
        identity_to_visible_company_id[merged.identity_key] = group.master_company_id

    for identity_key, card in card_by_identity.items():
        if identity_key in hidden_identities:
            continue
        visible_cards.append(card)
        identity_to_visible_company_id[identity_key] = clean_uuid_for_identity(identity_key)

    return visible_cards, identity_to_visible_company_id


def _assign_similar_company_ids(
    visible_cards: list[CanonicalCard],
    peer_map: dict[str, set[str]],
    merge_state: MergeState,
    identity_to_visible_company_id: dict[str, uuid.UUID],
) -> None:
    members_by_merge_identity = {
        build_merge_identity_key(group_id): set(members)
        for group_id, members in merge_state.members_by_group.items()
    }

    for card in visible_cards:
        source_identities = members_by_merge_identity.get(card.identity_key, {card.identity_key} if card.identity_key else set())
        similar_ids: set[str] = set()

        for identity in source_identities:
            for peer_identity in peer_map.get(identity, set()):
                peer_company_id = identity_to_visible_company_id.get(peer_identity)
                if peer_company_id is None:
                    continue
                similar_ids.add(str(peer_company_id))

        own_id = identity_to_visible_company_id.get(card.identity_key or "")
        if own_id is not None:
            similar_ids.discard(str(own_id))

        card.similar_company_ids = sorted(similar_ids)


def _company_id_for_card(card: CanonicalCard) -> uuid.UUID:
    if card.merge_group_id:
        return uuid.UUID(card.merge_group_id) if card.identity_key and card.identity_key.startswith(MERGE_IDENTITY_PREFIX) else clean_uuid_for_identity(card.identity_key or card.merge_group_id)
    return clean_uuid_for_identity(card.identity_key or f"fallback:{uuid.uuid4()}")


async def rebuild_clean_rows_for_week(
    session: AsyncSession,
    *,
    week_start: date,
    deduplicator: Deduplicator,
    risk_assessor: RiskAssessor,
) -> CleanRebuildResult:
    enriched_pairs = await load_week_enriched_pairs(session, week_start=week_start)
    if not enriched_pairs:
        await session.execute(delete(CompanyClean).where(CompanyClean.pipeline_week_start == week_start))
        await session.flush()
        return CleanRebuildResult(rows=[], total_reviews=0, source_total=0)

    pairs_for_clean = [pair for pair in enriched_pairs if pair[1].legal_verified] if settings.strict_legal_match else enriched_pairs
    pair_by_identity, card_by_identity, peer_map = _build_identity_maps(pairs_for_clean, deduplicator)
    merge_state = await load_merge_state(session)
    summary_overrides = await load_summary_overrides(session)

    visible_cards, identity_to_visible_company_id = _visible_cards_with_manual_merges(
        pair_by_identity,
        card_by_identity,
        merge_state,
        deduplicator,
    )
    _assign_similar_company_ids(visible_cards, peer_map, merge_state, identity_to_visible_company_id)

    db_id_map = {str(raw.id): raw for raw, _ in pairs_for_clean}
    extra_reviews_map: dict[str, list[RawReview]] = {}
    ai_sem = asyncio.Semaphore(max(1, settings.ai_workers))

    async def _process_card(card: CanonicalCard):
        reviews_for_ai = gather_reviews(card, db_id_map, extra_reviews_map)
        card.reviews_count = len(reviews_for_ai) if reviews_for_ai else 0
        if reviews_for_ai:
            ratings = [review["rating"] for review in reviews_for_ai if review.get("rating") is not None]
            if ratings:
                card.average_rating = round(sum(ratings) / len(ratings), 2)
        card.reviews_sample = reviews_for_ai[: settings.max_reviews_per_company]
        async with ai_sem:
            risk_level, risk_reasons = await risk_assessor.assess(
                checks=card.checks,
                reviews=reviews_for_ai,
                company_name=card.name_normalized,
            )
        summary = summary_overrides.get(card.identity_key or "")
        return card, summary, risk_level, risk_reasons, len(reviews_for_ai)

    processed = await asyncio.gather(*[_process_card(card) for card in visible_cards])

    await session.execute(delete(CompanyClean).where(CompanyClean.pipeline_week_start == week_start))

    rows: list[CompanyClean] = []
    total_reviews = 0
    for card, summary, risk_level, risk_reasons, review_count in processed:
        total_reviews += review_count
        row = CompanyClean(
            id=identity_to_visible_company_id.get(card.identity_key or "", clean_uuid_for_identity(card.identity_key or f"fallback:{uuid.uuid4()}")),
            identity_key=card.identity_key,
            merge_group_id=uuid.UUID(card.merge_group_id) if card.merge_group_id else None,
            inn=card.inn,
            ogrn=card.ogrn,
            name_normalized=card.name_normalized,
            source_name_primary=card.source_name_primary,
            legal_name=card.legal_name,
            legal_verified=card.legal_verified,
            relevance_score=card.relevance_score,
            geo_verified=card.geo_verified,
            quality_flags=card.quality_flags,
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
            source_links=card.source_links,
            similar_company_ids=card.similar_company_ids,
            checks=card.checks,
            manual_review_required=card.manual_review_required,
            pipeline_week_start=week_start,
        )
        session.add(row)
        rows.append(row)

    await session.flush()
    log.info(
        "clean_builder.done",
        week_start=str(week_start),
        source_pairs=len(pairs_for_clean),
        visible_rows=len(rows),
        total_reviews=total_reviews,
    )
    return CleanRebuildResult(rows=rows, total_reviews=total_reviews, source_total=len(pairs_for_clean))
