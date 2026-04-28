import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.clients.serper_client import search as serper_search
from src.db.models import Campaign, Query, SearchResult
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

SERPER_CONCURRENCY = 10


async def _try_acquire_credit(campaign_id: int) -> bool:
    """Atomically increment serper_credits_used iff under cap.
    Race-free: each successful UPDATE wins exactly one credit at the DB level.
    """
    async with SessionLocal() as s:
        stmt = (
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.serper_credits_used < Campaign.max_credits_serper,
            )
            .values(serper_credits_used=Campaign.serper_credits_used + 1)
            .returning(Campaign.id)
        )
        result = await s.execute(stmt)
        got = result.scalar_one_or_none() is not None
        await s.commit()
        return got


async def _persist_search_results(query_id: int, results: list, status: str) -> None:
    async with SessionLocal() as s:
        if results:
            s.add_all(
                [
                    SearchResult(
                        query_id=query_id,
                        url=r.url,
                        title=r.title,
                        snippet=r.snippet,
                        position=r.position,
                        status="new",
                    )
                    for r in results
                ]
            )
        await s.execute(update(Query).where(Query.id == query_id).values(status=status))
        await s.commit()


async def _mark_query_failed(query_id: int) -> None:
    async with SessionLocal() as s:
        await s.execute(update(Query).where(Query.id == query_id).values(status="failed"))
        await s.commit()


async def search_pending_queries(session: AsyncSession, campaign: Campaign) -> int:
    """Stage 2: run every `pending` query through Serper concurrently, persist results.

    - Each task opens its own SessionLocal (AsyncSession isn't safe under concurrent IO).
    - Budget: atomic UPDATE-WHERE on campaigns acquires at most `max_credits_serper` credits.
    - Idempotent: only `pending` queries are picked up; re-runs cost zero credits.
    - Halts and flips campaign.status='budget_exceeded' when budget runs out.

    Returns the count of queries successfully searched in this run.
    """
    sem = asyncio.Semaphore(SERPER_CONCURRENCY)
    budget_hit = asyncio.Event()
    campaign_id = campaign.id

    async def _run_one(query_id: int, query_text: str) -> int:
        if budget_hit.is_set():
            return 0

        async with sem:
            if budget_hit.is_set():
                return 0
            if not await _try_acquire_credit(campaign_id):
                budget_hit.set()
                return 0

            try:
                results = await serper_search(query_text)
            except Exception as e:
                logger.exception("serper search failed for query %d: %s", query_id, e)
                await _mark_query_failed(query_id)
                return 0

            await _persist_search_results(query_id, results, status="searched")
            return 1

    searched_total = 0

    while not budget_hit.is_set():
        # Pull a fresh batch of pending queries from the DB each pass.
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(Query.id, Query.query_text)
                    .where(Query.campaign_id == campaign_id, Query.status == "pending")
                    .limit(SERPER_CONCURRENCY)
                )
            ).all()
        if not rows:
            break

        results = await asyncio.gather(*[_run_one(qid, qt) for (qid, qt) in rows])
        searched_total += sum(results)

    if budget_hit.is_set():
        async with SessionLocal() as s:
            await s.execute(
                update(Campaign).where(Campaign.id == campaign_id).values(status="budget_exceeded")
            )
            await s.commit()
        logger.warning("campaign %s: stage 2 halted on Serper budget", campaign_id)

    # Refresh the caller's campaign instance so they see updated counters
    await session.refresh(campaign)
    logger.info(
        "stage 2 done for campaign %s: searched=%d, credits_used=%d/%d",
        campaign_id,
        searched_total,
        campaign.serper_credits_used,
        campaign.max_credits_serper,
    )
    return searched_total
