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
from src.pipeline.shared.subreddit_seed import seed_subreddits_for_campaign
from src.pipeline.whatsapp.extract import extract_for_campaign
from src.pipeline.whatsapp.queries import generate_queries

logger = logging.getLogger(__name__)

# A campaign already in one of these states should not be re-run.
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


async def run_whatsapp_campaign(campaign_id: int) -> None:
    """WhatsApp pipeline orchestrator: stages 1 → 6.

    Called by the platform dispatcher in `pipeline.run_campaign` when
    `Campaign.platform == "whatsapp"`. Other platforms (discord, slack) get
    their own orchestrators in their respective package folders.

    Budget semantics: hitting a paid stage's budget cap (Serper in stage 2,
    Firecrawl in stage 4b) sets status='budget_exceeded' on the campaign, but
    the orchestrator KEEPS GOING — stage 5 (extract is free regex) and stage 6
    (LLM scoring is bounded by lead count) run on whatever was already fetched.
    Final status stays 'budget_exceeded' so the operator sees the budget halt.

    Failure semantics: an unhandled exception flips status='failed' and aborts.

    Idempotent: each stage reads "what's still pending" from the DB. Re-enqueuing
    the same campaign_id resumes cleanly.
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
        # Each paid stage may flip status='budget_exceeded' internally; we don't
        # short-circuit on that — let cheap stages still produce leads from what
        # was fetched. We only stop early on 'failed' (caught below).

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

        # Cold-start subreddit mining — direct-search curated industry subs for
        # the platform anchor (chat.whatsapp.com / discord.gg / join.slack.com).
        # Inserts SearchResults tagged fetch_strategy='reddit' so the next stage
        # picks them up alongside Serper-routed Reddit URLs.
        await _set_stage(campaign_id, "seed_reddit")
        await seed_subreddits_for_campaign(campaign_id)

        await _set_stage(campaign_id, "fetch_reddit")
        await fetch_pending_reddit(campaign_id)

        await _set_stage(campaign_id, "fetch_web")
        await fetch_pending_firecrawl(campaign_id)

        await _set_stage(campaign_id, "extract")
        await extract_for_campaign(campaign_id)

        await _set_stage(campaign_id, "score")
        await score_for_campaign(campaign_id)

        await _finalize(campaign_id)
        logger.info("campaign %s: pipeline complete", campaign_id)

    except Exception as e:
        logger.exception("campaign %s: pipeline failed: %s", campaign_id, e)
        # Mark failed only if we're still 'running' — preserve budget_exceeded
        # if a stage already set it before the exception.
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
