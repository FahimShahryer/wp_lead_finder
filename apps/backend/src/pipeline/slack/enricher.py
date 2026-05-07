"""Slack enricher — load top leads, validate via the public landing page,
persist verified workspace name (or mark dead).

Mirrors `whatsapp.enricher` and `discord.enricher` shape so the API endpoint
can dispatch by platform without knowing what runs underneath.

Slack-specific notes:
  - Higher dead-rate is normal (tokens auto-expire ~30 days).
  - No description field — Slack landing pages don't expose one.
  - Snowball is the recovery mechanism: dead leads with KNOWN workspace
    names from a previous enrichment can be re-discovered with new tokens
    via slack.queries.snowball_from_verified_names.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select, update

from src.db.models import Lead, LeadTag
from src.db.session import SessionLocal
from src.pipeline.slack.validator import (
    DEFAULT_REQUEST_BUDGET,
    SlackBudgetExceeded,
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


# Same per-call cap as the other platforms — keep response shape consistent
# across the whole API surface.
MAX_LEADS_PER_RUN = 10


async def enrich_campaign(
    campaign_id: int,
    *,
    only_unvalidated: bool = True,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
    limit: int = MAX_LEADS_PER_RUN,
) -> EnrichmentCounts:
    """Validate the top-N leads of a Slack campaign by fetching each invite's
    public landing page, then persist the recovered workspace name."""
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

    invite_ids = [ld.invite_id for ld in leads]
    info_by_id = await fetch_invites(invite_ids, request_budget=request_budget)

    now = datetime.now(timezone.utc)
    valid = invalid = transient = 0

    async with SessionLocal() as s:
        for ld in leads:
            info = info_by_id.get(ld.invite_id)
            if info is None:
                # Transient — skip persistence so retries can try again.
                transient += 1
                continue
            if info.valid:
                valid += 1
            else:
                invalid += 1
                # Dead invite: drop tags. Same logic as WA/Discord enrichers.
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
    logger.info("slack enricher: campaign %s done: %s", campaign_id, counts)
    return counts


__all__ = [
    "EnrichmentCounts",
    "enrich_campaign",
    "SlackBudgetExceeded",
    "DEFAULT_REQUEST_BUDGET",
]
