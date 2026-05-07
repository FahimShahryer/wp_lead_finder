"""Slack pipeline orchestrator — stages 1 → 6 for a Slack campaign.

Mirrors WhatsApp's and Discord's orchestrators exactly. The only platform-
specific calls are stage 1 (Slack-anchored queries) and stage 5 (Slack
extract regex). Stages 2-4 + 6 use the shared modules.

Validation (the equivalent of stage 7 in the WA flow) is NOT in the
orchestrator — it's a manually-triggered API endpoint, same as WA/Discord.
For Slack we strongly recommend enriching within 24-48h of extraction
because tokens auto-expire.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import update

from src.db.models import Campaign
from src.db.session import SessionLocal
from src.pipeline.shared.firecrawl_fetch import fetch_pending_firecrawl
from src.pipeline.shared.prefilter import prefilter_search_results
from src.pipeline.shared.reddit_fetch import fetch_pending_reddit
from src.pipeline.shared.score import score_for_campaign
from src.pipeline.shared.serper_search import search_pending_queries
from src.pipeline.slack.extract import extract_for_campaign
from src.pipeline.slack.queries import generate_queries

logger = logging.getLogger(__name__)


TERMINAL_STATUSES = ("done", "budget_exceeded", "failed")


async def _set_stage(campaign_id: int, stage: str | None) -> None:
    async with SessionLocal() as s:
        await s.execute(
            update(Campaign).where(Campaign.id == campaign_id).values(current_stage=stage)
        )
        await s.commit()


async def _campaign_status(campaign_id: int) -> str | None:
    async with SessionLocal() as s:
        c = await s.get(Campaign, campaign_id)
    return c.status if c else None


async def _finalize(campaign_id: int) -> None:
    """Always called at end of run. Preserves 'budget_exceeded' / 'failed' if
    set; otherwise transitions 'running' → 'done'. Always clears current_stage
    and stamps completed_at."""
    async with SessionLocal() as s:
        c = await s.get(Campaign, campaign_id)
        if c is None:
            return
        new_status = "done" if c.status == "running" else c.status
        await s.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(
                status=new_status,
                current_stage=None,
                completed_at=c.completed_at or datetime.now(timezone.utc),
            )
        )
        await s.commit()


async def run_slack_campaign(campaign_id: int) -> None:
    """Slack pipeline orchestrator: stages 1 → 6.

    Called by the platform dispatcher in `pipeline.run_campaign` when
    `Campaign.platform == "slack"`.

    Same budget / failure / idempotency semantics as the WhatsApp and
    Discord orchestrators.
    """
    async with SessionLocal() as s:
        campaign = await s.get(Campaign, campaign_id)
        if campaign is None:
            raise ValueError(f"campaign {campaign_id} not found")
        if campaign.status in TERMINAL_STATUSES:
            logger.info("campaign %s already %s; skipping", campaign_id, campaign.status)
            return
        if campaign.status == "queued":
            campaign.status = "running"
            await s.commit()

    try:
        await _set_stage(campaign_id, "queries")
        async with SessionLocal() as s:
            c = await s.get(Campaign, campaign_id)
            await generate_queries(s, c)

        await _set_stage(campaign_id, "search")
        async with SessionLocal() as s:
            c = await s.get(Campaign, campaign_id)
            await search_pending_queries(s, c)

        await _set_stage(campaign_id, "prefilter")
        async with SessionLocal() as s:
            await prefilter_search_results(s, campaign_id)

        await _set_stage(campaign_id, "fetch_reddit")
        await fetch_pending_reddit(campaign_id)

        await _set_stage(campaign_id, "fetch_web")
        await fetch_pending_firecrawl(campaign_id)

        await _set_stage(campaign_id, "extract")
        await extract_for_campaign(campaign_id)

        await _set_stage(campaign_id, "score")
        await score_for_campaign(campaign_id)

        await _finalize(campaign_id)
        logger.info("campaign %s (slack): pipeline complete", campaign_id)

    except Exception as e:
        logger.exception("campaign %s (slack): pipeline failed: %s", campaign_id, e)
        if await _campaign_status(campaign_id) == "running":
            async with SessionLocal() as s:
                await s.execute(
                    update(Campaign)
                    .where(Campaign.id == campaign_id)
                    .values(
                        status="failed",
                        current_stage=None,
                        completed_at=datetime.now(timezone.utc),
                    )
                )
                await s.commit()
        else:
            await _finalize(campaign_id)
        raise
