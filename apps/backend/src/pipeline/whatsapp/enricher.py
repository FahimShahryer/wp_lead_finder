import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from redis.asyncio import Redis
from sqlalchemy import delete, select, update

from src.db.models import Lead, LeadTag
from src.db.session import SessionLocal
from src.pipeline.whatsapp.validator import (
    DEFAULT_REQUEST_BUDGET,
    WhatsAppBlocked,
    WhatsAppBudgetExceeded,
    fetch_invites,
)

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentCounts:
    considered: int
    fetched: int  # came back from cache or network with a definitive result
    valid: int  # invite was live (group name resolved)
    invalid: int  # invite confirmed dead
    transient_errors: int  # network blip — not persisted, retry next run


MAX_LEADS_PER_RUN = 10


async def enrich_campaign(
    campaign_id: int,
    redis: Redis,
    *,
    only_unvalidated: bool = True,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
    limit: int = MAX_LEADS_PER_RUN,
) -> EnrichmentCounts:
    """Hit WhatsApp's invite landing page for each lead, persist the verified
    group name + description (or mark dead-link). By default skips leads that
    were already validated. Processes at most `limit` leads per call (1..10),
    highest total_score first — to avoid burning WhatsApp request budget /
    triggering rate-limits on a giant batch. May raise:
      - WhatsAppBlocked: Meta returned a CAPTCHA/challenge mid-batch. Caller
        should retry later (probably from a different IP).
      - WhatsAppBudgetExceeded: more leads than the per-run request budget.
    """
    if not 1 <= limit <= MAX_LEADS_PER_RUN:
        raise ValueError(f"limit must be between 1 and {MAX_LEADS_PER_RUN}, got {limit}")
    async with SessionLocal() as s:
        stmt = select(Lead).where(Lead.campaign_id == campaign_id)
        if only_unvalidated:
            stmt = stmt.where(Lead.last_validated_at.is_(None))
        # Highest-value leads first; NULL scores last.
        stmt = stmt.order_by(Lead.total_score.desc().nullslast()).limit(limit)
        leads = list((await s.execute(stmt)).scalars())

    if not leads:
        return EnrichmentCounts(0, 0, 0, 0, 0)

    invite_ids = [ld.invite_id for ld in leads]
    info_by_id = await fetch_invites(invite_ids, redis, request_budget=request_budget)

    now = datetime.now(timezone.utc)
    valid = invalid = transient = 0

    async with SessionLocal() as s:
        for ld in leads:
            info = info_by_id.get(ld.invite_id)
            if info is None:
                # Transient network error — skip persistence so retries can
                # try again. We do NOT bump last_validated_at.
                transient += 1
                continue
            if info.valid:
                valid += 1
            else:
                invalid += 1
                # Dead invite: drop any tags that may have been assigned
                # earlier (from auto_tag). Keeps tag data consistent — tags
                # describe joinable groups, not dead links.
                await s.execute(
                    delete(LeadTag).where(LeadTag.lead_id == ld.id)
                )
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
    logger.info("wa_enricher: campaign %s done: %s", campaign_id, counts)
    return counts


# Re-export the exception types so endpoints can import from one place.
__all__ = [
    "EnrichmentCounts",
    "enrich_campaign",
    "WhatsAppBlocked",
    "WhatsAppBudgetExceeded",
]
