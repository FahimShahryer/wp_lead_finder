"""Discord stage 1 — query generation entry point.

Same shape as the WhatsApp wrapper, just with a Discord anchor
(`discord.gg`). All algorithm logic lives in `shared/query_builders.py`.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Campaign, Lead, Query
from src.db.session import SessionLocal
from src.pipeline.shared.query_builders import (
    MAX_GROUP_NAME_AS_TERM_LEN,
    SNOWBALL_DEFAULT_TARGET,
    _resolve_query_platforms,
    build_queries,
    expand_industry_terms,
    query_target_count,
)

logger = logging.getLogger(__name__)


# Discord's most-common public-invite host. We pick `discord.gg` over the
# longer `discord.com/invite` because (1) it's by far more common in posts,
# (2) Google indexes the short form fine, and (3) keeping a single anchor
# halves query duplication. The extractor in discord.extract still matches
# all three URL forms (gg / com/invite / discordapp.com/invite) — the anchor
# is only about query-construction precision, not extraction recall.
ANCHOR = "discord.gg"


async def generate_queries(session: AsyncSession, campaign: Campaign) -> list[Query]:
    """Stage 1: ICP -> N queries persisted in `queries` (status='pending')."""
    existing_count = await session.scalar(
        select(func.count(Query.id)).where(Query.campaign_id == campaign.id)
    )
    if existing_count and existing_count > 0:
        logger.info(
            "campaign %s already has %d queries, skipping 1A+1B",
            campaign.id, existing_count,
        )
        result = await session.execute(select(Query).where(Query.campaign_id == campaign.id))
        return list(result.scalars().all())

    industries = [s.strip() for s in (campaign.industries or []) if s and s.strip()]
    if not industries:
        logger.warning("campaign %s has no industries — generating no queries", campaign.id)
        return []

    seen_terms: set[str] = set()
    all_terms: list[str] = []
    for industry in industries:
        for term in await expand_industry_terms(industry):
            key = term.lower()
            if key in seen_terms:
                continue
            seen_terms.add(key)
            all_terms.append(term)

    platforms = _resolve_query_platforms(campaign.platforms)
    target = query_target_count(campaign.max_credits_serper)
    pairs = build_queries(
        all_terms, platforms, campaign.negative_locations, target, anchor=ANCHOR
    )

    rows = [
        Query(
            campaign_id=campaign.id,
            query_text=q,
            source_platform=src,
            status="pending",
        )
        for q, src in pairs
    ]
    session.add_all(rows)
    await session.commit()
    logger.info(
        "stage 1 (discord) done for campaign %s: %d terms × %d platforms → %d queries",
        campaign.id, len(all_terms), len(platforms), len(rows),
    )
    return rows


async def snowball_from_verified_names(
    campaign_id: int, top_n: int = 10, target_count: int = SNOWBALL_DEFAULT_TARGET
) -> int:
    """Take the top-N enriched leads (highest total_score, has
    verified_group_name = the Discord server name) and feed those names back
    into 1B as new term seeds. Returns count of NEW queries inserted.
    """
    async with SessionLocal() as s:
        leads = list(
            (
                await s.execute(
                    select(Lead)
                    .where(
                        Lead.campaign_id == campaign_id,
                        Lead.verified_group_name.isnot(None),
                        Lead.total_score.isnot(None),
                    )
                    .order_by(Lead.total_score.desc())
                    .limit(top_n)
                )
            ).scalars()
        )

    if not leads:
        logger.info("snowball: no enriched leads for campaign %s", campaign_id)
        return 0

    seen: set[str] = set()
    terms: list[str] = []
    for ld in leads:
        name = (ld.verified_group_name or "").strip()
        if not name or len(name) > MAX_GROUP_NAME_AS_TERM_LEN:
            continue
        name = name.replace('"', "")
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        terms.append(name)

    if not terms:
        logger.info("snowball: no usable server names for campaign %s", campaign_id)
        return 0

    async with SessionLocal() as s:
        campaign = await s.get(Campaign, campaign_id)
        if campaign is None:
            return 0
        platforms = _resolve_query_platforms(campaign.platforms)
        pairs = build_queries(
            terms, platforms, campaign.negative_locations, target_count, anchor=ANCHOR
        )

        existing_set = {
            row[0]
            for row in (
                await s.execute(
                    select(Query.query_text).where(Query.campaign_id == campaign_id)
                )
            ).all()
        }
        new_pairs = [(q, src) for q, src in pairs if q not in existing_set]

        if not new_pairs:
            logger.info("snowball: all %d candidate queries already exist", len(pairs))
            return 0

        rows = [
            Query(
                campaign_id=campaign_id,
                query_text=q,
                source_platform=src,
                status="pending",
            )
            for q, src in new_pairs
        ]
        s.add_all(rows)
        await s.commit()

    logger.info(
        "snowball (discord): campaign %s seeded %d new queries from %d names",
        campaign_id, len(new_pairs), len(terms),
    )
    return len(new_pairs)
