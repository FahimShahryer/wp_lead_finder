"""Discord enricher — load top leads, validate via Discord API, persist
verified server name + description (or mark dead).

Mirrors `whatsapp.enricher` shape so the API endpoint can route by platform
without caring about which validator runs underneath.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select, update

from src.db.models import Lead, LeadTag
from src.db.session import SessionLocal
from src.pipeline.discord.validator import (
    DEFAULT_REQUEST_BUDGET,
    DiscordBudgetExceeded,
    fetch_invites,
)

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentCounts:
    considered: int
    fetched: int
    valid: int
    invalid: int
    transient_errors: int


# Same per-call cap as WA — keep response shape and rate-limit-friendliness
# consistent across platforms.
MAX_LEADS_PER_RUN = 10


async def enrich_campaign(
    campaign_id: int,
    *,
    only_unvalidated: bool = True,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
    limit: int = MAX_LEADS_PER_RUN,
) -> EnrichmentCounts:
    """Validate the top-N leads of a Discord campaign via Discord's public API,
    persist verified server names. Mirrors the WA enricher's interface.

    Discord's API is unauthenticated and high-throughput, so unlike WA there's
    no Redis-backed rate limiter; just bounded async fan-out inside the
    validator.
    """
    if not 1 <= limit <= MAX_LEADS_PER_RUN:
        raise ValueError(f"limit must be between 1 and {MAX_LEADS_PER_RUN}, got {limit}")

    async with SessionLocal() as s:
        stmt = select(Lead).where(Lead.campaign_id == campaign_id)
        if only_unvalidated:
            stmt = stmt.where(Lead.last_validated_at.is_(None))
        stmt = stmt.order_by(Lead.total_score.desc().nullslast()).limit(limit)
        leads = list((await s.execute(stmt)).scalars())

    if not leads:
        return EnrichmentCounts(0, 0, 0, 0, 0)

    invite_codes = [ld.invite_id for ld in leads]
    info_by_code = await fetch_invites(invite_codes, request_budget=request_budget)

    now = datetime.now(timezone.utc)
    valid = invalid = transient = 0

    async with SessionLocal() as s:
        for ld in leads:
            info = info_by_code.get(ld.invite_id)
            if info is None:
                # Transient — skip persistence so retries can try again.
                transient += 1
                continue
            if info.valid:
                valid += 1
            else:
                invalid += 1
                # Dead invite: drop tags. Same logic as the WA enricher.
                await s.execute(delete(LeadTag).where(LeadTag.lead_id == ld.id))
            await s.execute(
                update(Lead)
                .where(Lead.id == ld.id)
                .values(
                    verified_group_name=info.group_name,
                    verified_group_description=info.group_description,
                    last_validated_at=now,
                )
            )
        await s.commit()

    counts = EnrichmentCounts(
        considered=len(leads),
        fetched=valid + invalid,
        valid=valid,
        invalid=invalid,
        transient_errors=transient,
    )
    logger.info("discord enricher: campaign %s done: %s", campaign_id, counts)
    return counts


__all__ = [
    "EnrichmentCounts",
    "enrich_campaign",
    "DiscordBudgetExceeded",
    "DEFAULT_REQUEST_BUDGET",
]
