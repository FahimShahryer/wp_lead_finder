import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.clients.firecrawl_client import FirecrawlPermanentError, scrape_markdown
from src.db.models import Campaign, Query, SearchResult, UrlCache
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

# Firecrawl plan caps concurrent browsers (typically 2 on Hobby, 5+ on paid).
# Setting this above your plan's cap just queues at Firecrawl's side, no speedup.
# Override per-deployment if you upgrade.
FIRECRAWL_CONCURRENCY = 2
CACHE_TTL = timedelta(days=7)


async def _get_fresh_cached_markdown(url: str) -> str | None:
    async with SessionLocal() as s:
        cached = await s.get(UrlCache, url)
        if cached is None:
            return None
        if cached.fetched_at < datetime.now(timezone.utc) - CACHE_TTL:
            return None
        return cached.markdown


async def _try_acquire_credit(campaign_id: int) -> bool:
    """Atomic UPDATE-WHERE budget acquire, identical pattern to stage 2 Serper."""
    async with SessionLocal() as s:
        stmt = (
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.firecrawl_credits_used < Campaign.max_credits_firecrawl,
            )
            .values(firecrawl_credits_used=Campaign.firecrawl_credits_used + 1)
            .returning(Campaign.id)
        )
        got = (await s.execute(stmt)).scalar_one_or_none() is not None
        await s.commit()
        return got


async def _upsert_cache(url: str, markdown: str) -> None:
    async with SessionLocal() as s:
        stmt = pg_insert(UrlCache).values(url=url, markdown=markdown)
        stmt = stmt.on_conflict_do_update(
            index_elements=["url"],
            set_={"markdown": stmt.excluded.markdown, "fetched_at": datetime.now(timezone.utc)},
        )
        await s.execute(stmt)
        await s.commit()


async def _set_status(sr_id: int, status: str) -> None:
    async with SessionLocal() as s:
        await s.execute(update(SearchResult).where(SearchResult.id == sr_id).values(status=status))
        await s.commit()


async def fetch_pending_firecrawl(campaign_id: int) -> dict[str, int]:
    """Stage 4b: fetch every web-tagged search_result that hasn't been fetched yet.

    - Cache hit (fresh) → no API call, no credit spend, status='fetched'.
    - Cache miss → atomic credit acquire → Firecrawl scrape → upsert cache → status='fetched'.
    - Permanent error (site not supported, paywall) → status='fetch_failed', credit already spent.
    - Budget exceeded → halt, flip campaign.status='budget_exceeded', remaining rows untouched.

    Idempotent: only fetch_strategy='web' AND status='new' rows.
    """
    sem = asyncio.Semaphore(FIRECRAWL_CONCURRENCY)
    budget_hit = asyncio.Event()
    counts = {"cache_hit": 0, "fetched": 0, "failed": 0, "budget_skipped": 0}

    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(SearchResult.id, SearchResult.url)
                .join(Query, SearchResult.query_id == Query.id)
                .where(
                    Query.campaign_id == campaign_id,
                    SearchResult.fetch_strategy == "web",
                    SearchResult.status == "new",
                )
            )
        ).all()

    if not rows:
        logger.info("stage 4b: no pending web rows for campaign %s", campaign_id)
        return counts

    async def _run_one(sr_id: int, url: str) -> None:
        if budget_hit.is_set():
            counts["budget_skipped"] += 1
            return

        async with sem:
            if budget_hit.is_set():
                counts["budget_skipped"] += 1
                return

            cached = await _get_fresh_cached_markdown(url)
            if cached is not None:
                counts["cache_hit"] += 1
                await _set_status(sr_id, "fetched")
                return

            if not await _try_acquire_credit(campaign_id):
                budget_hit.set()
                counts["budget_skipped"] += 1
                return

            try:
                markdown = await scrape_markdown(url)
            except FirecrawlPermanentError as e:
                logger.info("firecrawl fetch_failed sr=%d url=%s reason=%s", sr_id, url, e.reason)
                counts["failed"] += 1
                await _set_status(sr_id, "fetch_failed")
                return
            except Exception as e:
                logger.exception("firecrawl unexpected error sr=%d url=%s: %s", sr_id, url, e)
                counts["failed"] += 1
                await _set_status(sr_id, "fetch_failed")
                return

            await _upsert_cache(url, markdown)
            await _set_status(sr_id, "fetched")
            counts["fetched"] += 1

    await asyncio.gather(*[_run_one(sr_id, url) for (sr_id, url) in rows])

    if budget_hit.is_set():
        async with SessionLocal() as s:
            await s.execute(
                update(Campaign).where(Campaign.id == campaign_id).values(status="budget_exceeded")
            )
            await s.commit()
        logger.warning("campaign %s: stage 4b halted on Firecrawl budget", campaign_id)

    logger.info("stage 4b done for campaign %s: %s", campaign_id, counts)
    return counts
