import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.clients.reddit_client import (
    RedditFetchError,
    RedditUnreachable,
    fetch_submission_markdown,
    reddit_session,
)
from src.db.models import Query, SearchResult, UrlCache
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

REDDIT_CONCURRENCY = 3
CACHE_TTL = timedelta(days=7)


async def _get_fresh_cached_markdown(url: str) -> str | None:
    """Returns cached markdown if present AND newer than CACHE_TTL, else None."""
    async with SessionLocal() as s:
        cached = await s.get(UrlCache, url)
        if cached is None:
            return None
        cutoff = datetime.now(timezone.utc) - CACHE_TTL
        if cached.fetched_at < cutoff:
            return None
        return cached.markdown


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


async def fetch_pending_reddit(campaign_id: int) -> dict[str, int]:
    """Stage 4a: fetch every reddit-tagged search_result that hasn't been fetched yet.

    For each row:
      - cache hit (fresh) → use it, set status='fetched', no API call.
      - cache miss → asyncpraw, render markdown, upsert cache, set status='fetched'.
      - permanent error → set status='fetch_failed' with reason; campaign continues.

    If Reddit's API itself is unreachable (auth times out), the stage records
    `unreachable` for every pending row and returns; the orchestrator continues
    to stage 4b. Unreached rows stay status='new' so a future run can retry them.

    Idempotent: only picks up fetch_strategy='reddit' AND status='new' rows.
    """
    sem = asyncio.Semaphore(REDDIT_CONCURRENCY)
    counts = {"cache_hit": 0, "fetched": 0, "failed": 0, "unreachable": 0}

    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(SearchResult.id, SearchResult.url)
                .join(Query, SearchResult.query_id == Query.id)
                .where(
                    Query.campaign_id == campaign_id,
                    SearchResult.fetch_strategy == "reddit",
                    SearchResult.status == "new",
                )
            )
        ).all()

    if not rows:
        logger.info("stage 4a: no pending reddit rows for campaign %s", campaign_id)
        return counts

    try:
        async with reddit_session() as reddit:

            async def _run_one(sr_id: int, url: str) -> None:
                async with sem:
                    cached = await _get_fresh_cached_markdown(url)
                    if cached is not None:
                        counts["cache_hit"] += 1
                        await _set_status(sr_id, "fetched")
                        return

                    try:
                        markdown = await fetch_submission_markdown(reddit, url)
                    except RedditUnreachable as e:
                        # Per-URL network failure — leave row 'new' so a retry
                        # picks it up later. Don't count this as a hard failure.
                        logger.info("reddit unreachable sr=%d url=%s: %s", sr_id, url, e)
                        counts["unreachable"] += 1
                        return
                    except RedditFetchError as e:
                        logger.info(
                            "reddit fetch_failed sr=%d url=%s reason=%s",
                            sr_id, url, e.reason,
                        )
                        counts["failed"] += 1
                        await _set_status(sr_id, "fetch_failed")
                        return
                    except Exception as e:
                        logger.exception(
                            "reddit unexpected error sr=%d url=%s: %s", sr_id, url, e
                        )
                        counts["failed"] += 1
                        await _set_status(sr_id, "fetch_failed")
                        return

                    await _upsert_cache(url, markdown)
                    await _set_status(sr_id, "fetched")
                    counts["fetched"] += 1

            await asyncio.gather(*[_run_one(sr_id, url) for (sr_id, url) in rows])
    except RedditUnreachable as e:
        logger.warning("stage 4a: Reddit unreachable, skipping (%s)", e)
        counts["unreachable"] = len(rows)
        return counts

    logger.info("stage 4a done for campaign %s: %s", campaign_id, counts)
    return counts
