import os
from datetime import datetime, timedelta, timezone

import pytest

from src.db.models import Campaign, Query, SearchResult, UrlCache
from src.db.session import SessionLocal
from src.pipeline.shared.firecrawl_fetch import fetch_pending_firecrawl

pytestmark = pytest.mark.skipif(
    not os.getenv("FIRECRAWL_API_KEY"),
    reason="FIRECRAWL_API_KEY not set; skipping live Firecrawl test",
)


# Two real, well-known web pages — neither is reddit/social/file. Cheap to scrape.
REAL_URLS = [
    "https://example.com",
    "https://httpbin.org/html",
]


async def _seed_campaign(
    urls: list[tuple[str, str]],
    *,
    max_credits_firecrawl: int = 200,
) -> int:
    async with SessionLocal() as s:
        c = Campaign(
            name="step7",
            industries=["x"],
            max_credits_firecrawl=max_credits_firecrawl,
        )
        s.add(c)
        await s.flush()
        q = Query(campaign_id=c.id, query_text="probe", source_platform="web", status="searched")
        s.add(q)
        await s.flush()
        for url, strategy in urls:
            s.add(
                SearchResult(
                    query_id=q.id, url=url, status="new", fetch_strategy=strategy
                )
            )
        await s.commit()
        return c.id


async def test_fetches_real_web_urls_and_caches_markdown():
    cid = await _seed_campaign([(u, "web") for u in REAL_URLS])

    counts = await fetch_pending_firecrawl(cid)

    succeeded = counts["fetched"] + counts["cache_hit"]
    assert succeeded == len(REAL_URLS), f"all URLs should resolve, got {counts}"

    async with SessionLocal() as s:
        cache_rows = list((await s.execute(__import__("sqlalchemy").select(UrlCache))).scalars())
        campaign = await s.get(Campaign, cid)

    assert len(cache_rows) == len(REAL_URLS)
    for cr in cache_rows:
        assert cr.markdown.strip(), f"empty markdown for {cr.url}"

    # Each fresh fetch costs exactly 1 Firecrawl credit.
    assert campaign.firecrawl_credits_used == counts["fetched"]


async def test_fresh_cache_hit_skips_firecrawl_call_and_credit():
    sentinel_url = "https://sentinel.example.invalid/foo"
    sentinel_md = "# SENTINEL — must not be overwritten\n"
    locked_in = datetime.now(timezone.utc) - timedelta(hours=1)

    async with SessionLocal() as s:
        s.add(UrlCache(url=sentinel_url, markdown=sentinel_md, fetched_at=locked_in))
        await s.commit()

    cid = await _seed_campaign([(sentinel_url, "web")])

    counts = await fetch_pending_firecrawl(cid)

    assert counts == {"cache_hit": 1, "fetched": 0, "failed": 0, "budget_skipped": 0}

    async with SessionLocal() as s:
        cached = await s.get(UrlCache, sentinel_url)
        campaign = await s.get(Campaign, cid)

    assert cached.markdown == sentinel_md, "cache markdown was overwritten"
    assert abs((cached.fetched_at - locked_in).total_seconds()) < 1
    assert campaign.firecrawl_credits_used == 0, "cache hit must not spend a credit"


async def test_budget_guard_halts_run_and_flips_status():
    # 2 web URLs, budget=1 → at most 1 should be fetched.
    cid = await _seed_campaign(
        [(u, "web") for u in REAL_URLS],
        max_credits_firecrawl=1,
    )

    counts = await fetch_pending_firecrawl(cid)

    async with SessionLocal() as s:
        campaign = await s.get(Campaign, cid)

    assert campaign.firecrawl_credits_used <= 1
    assert counts["fetched"] + counts["cache_hit"] <= 1
    assert counts["budget_skipped"] >= 1
    assert campaign.status == "budget_exceeded"


async def test_idempotent_rerun_processes_zero_rows():
    cid = await _seed_campaign([(REAL_URLS[0], "web")])

    first = await fetch_pending_firecrawl(cid)
    second = await fetch_pending_firecrawl(cid)

    assert first["fetched"] + first["cache_hit"] == 1
    assert second == {"cache_hit": 0, "fetched": 0, "failed": 0, "budget_skipped": 0}
