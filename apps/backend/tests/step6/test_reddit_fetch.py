import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.clients.reddit_client import RedditUnreachable, reddit_session
from src.db.models import Campaign, Query, SearchResult, UrlCache
from src.db.session import SessionLocal
from src.pipeline.stage4a_reddit import fetch_pending_reddit

# Live Reddit test — gated on creds present in container env.
pytestmark = pytest.mark.skipif(
    not (os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET")),
    reason="REDDIT_CLIENT_ID/SECRET not set; skipping live Reddit test",
)


@pytest.fixture(autouse=True)
async def _verify_reddit_reachable():
    """Per-test pre-flight: skip when Reddit's API is currently blocking us
    (rate limit / TLS handshake failure). Reddit's bot detection is intermittent
    so this needs to re-check before every test, not once per module."""
    try:
        async with reddit_session():
            pass
    except RedditUnreachable as e:
        pytest.skip(f"Reddit API not reachable: {e}")


def _skip_if_unreachable(counts: dict[str, int]) -> None:
    """Mid-test defense: if reddit_session succeeded at start but the actual
    fetch_pending_reddit call hit unreachability, skip cleanly."""
    if counts.get("unreachable", 0) > 0:
        pytest.skip(f"Reddit went unreachable mid-test: {counts}")


# Real Reddit submissions about WhatsApp groups + AI/marketing agencies.
# Posts on /r/announcements-style or aged threads are unlikely to vanish, but
# if Reddit deletes any, the test will mark them fetch_failed (expected).
REAL_URLS = [
    "https://www.reddit.com/r/MarketingAutomation/comments/1hbqfxk/free_whatsapp_group_for_ai_automation_agency/",
    "https://www.reddit.com/r/SaaS/comments/1nig68r/ai_assistant_that_drafts_whatsapp_replies_for/",
    "https://www.reddit.com/r/n8n/comments/1qnz10y/whatsapp_with_ai_agent_coexistence/",
]

# NOT a /r/sub/comments/id/... URL — asyncpraw can't parse it as a submission,
# which exercises the catch-all error path.
INVALID_URL = "https://www.reddit.com/this_is_not_a_valid_submission_url_at_all"


async def _seed_campaign(urls_with_strategy: list[tuple[str, str]]) -> int:
    async with SessionLocal() as s:
        c = Campaign(name="step6", industries=["x"])
        s.add(c)
        await s.flush()
        q = Query(campaign_id=c.id, query_text="probe", source_platform="reddit", status="searched")
        s.add(q)
        await s.flush()
        for url, strategy in urls_with_strategy:
            s.add(
                SearchResult(
                    query_id=q.id,
                    url=url,
                    status="new",
                    fetch_strategy=strategy,
                )
            )
        await s.commit()
        return c.id


async def test_fetches_real_reddit_urls_and_caches_markdown():
    cid = await _seed_campaign([(u, "reddit") for u in REAL_URLS])

    counts = await fetch_pending_reddit(cid)
    _skip_if_unreachable(counts)

    # All three either fetched OR cached; at minimum 2/3 should succeed
    # (Reddit may have deleted one in the wild — we tolerate that).
    succeeded = counts["fetched"] + counts["cache_hit"]
    assert succeeded >= 2, f"expected >=2 successes, got {counts}"

    async with SessionLocal() as s:
        cache_rows = (await s.execute(select(UrlCache))).scalars().all()
        sr_rows = (
            await s.execute(
                select(SearchResult)
                .join(Query)
                .where(Query.campaign_id == cid)
            )
        ).scalars().all()

    assert len(cache_rows) >= 2, f"expected >=2 url_cache rows, got {len(cache_rows)}"
    for cr in cache_rows:
        assert cr.markdown.strip(), f"empty markdown for {cr.url}"
        # Reddit submissions render with a "# title" heading — sanity-check.
        assert cr.markdown.startswith("#"), cr.markdown[:80]

    fetched_srs = [sr for sr in sr_rows if sr.status == "fetched"]
    assert len(fetched_srs) >= 2


async def test_invalid_reddit_url_marks_fetch_failed_without_crashing():
    cid = await _seed_campaign([(INVALID_URL, "reddit")])

    counts = await fetch_pending_reddit(cid)
    _skip_if_unreachable(counts)

    assert counts["failed"] == 1
    assert counts["fetched"] == 0

    async with SessionLocal() as s:
        sr = (
            await s.execute(
                select(SearchResult).join(Query).where(Query.campaign_id == cid)
            )
        ).scalar_one()
    assert sr.status == "fetch_failed"


async def test_fresh_cache_skips_api_call():
    """If url_cache already has fresh markdown for a URL, stage 4a must NOT call asyncpraw.
    Verified via:
      - Pre-populate cache with sentinel markdown.
      - Run stage 4a.
      - Assert cache row's `fetched_at` did not advance and markdown unchanged."""
    sentinel_url = "https://www.reddit.com/r/some_test_subreddit/comments/sentinel_id/x/"
    sentinel_md = "# SENTINEL — must not be overwritten\n"
    locked_in = datetime.now(timezone.utc) - timedelta(hours=1)  # 1h old, well within 7-day TTL

    async with SessionLocal() as s:
        s.add(UrlCache(url=sentinel_url, markdown=sentinel_md, fetched_at=locked_in))
        await s.commit()

    cid = await _seed_campaign([(sentinel_url, "reddit")])

    counts = await fetch_pending_reddit(cid)
    assert counts["cache_hit"] == 1
    assert counts["fetched"] == 0
    assert counts["failed"] == 0

    async with SessionLocal() as s:
        cached = await s.get(UrlCache, sentinel_url)
    assert cached.markdown == sentinel_md, "cache markdown was overwritten"
    # Allow ms-level tolerance; the row should NOT have been UPSERTed.
    assert abs((cached.fetched_at - locked_in).total_seconds()) < 1


async def test_idempotent_rerun_processes_zero_rows():
    cid = await _seed_campaign([(REAL_URLS[0], "reddit")])

    first = await fetch_pending_reddit(cid)
    _skip_if_unreachable(first)
    second = await fetch_pending_reddit(cid)
    _skip_if_unreachable(second)

    assert first["fetched"] + first["cache_hit"] == 1
    # On second run, the search_result is already 'fetched' so it's not picked up at all.
    assert second == {"cache_hit": 0, "fetched": 0, "failed": 0, "unreachable": 0}
