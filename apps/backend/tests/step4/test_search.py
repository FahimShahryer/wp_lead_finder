import os

import pytest
from sqlalchemy import select, func

from src.db.models import Campaign, Query, SearchResult
from src.db.session import SessionLocal
from src.pipeline.shared.serper_search import search_pending_queries

# Live Serper test — gated on the key being injected into the api container.
pytestmark = pytest.mark.skipif(
    not os.getenv("SERPER_API_KEY"),
    reason="SERPER_API_KEY not set in container env; skipping live Serper test",
)


SAMPLE_QUERIES = [
    'site:reddit.com whatsapp group "AI agency" -india',
    'site:reddit.com "marketing agency owners" whatsapp community -india',
    'site:reddit.com "SaaS founders" whatsapp join -india',
]


async def _seed_campaign_with_queries(queries: list[str], max_credits: int = 500) -> int:
    async with SessionLocal() as s:
        c = Campaign(
            name="step4-search-test",
            industries=["AI agency owners"],
            locations=["US", "UK"],
            negative_locations=["India"],
            platforms=["reddit"],
            max_credits_serper=max_credits,
        )
        s.add(c)
        await s.flush()
        for qt in queries:
            s.add(Query(campaign_id=c.id, query_text=qt, source_platform="reddit", status="pending"))
        await s.commit()
        return c.id


async def test_serper_search_persists_results_for_3_queries():
    cid = await _seed_campaign_with_queries(SAMPLE_QUERIES)

    async with SessionLocal() as s:
        campaign = await s.get(Campaign, cid)
        searched = await search_pending_queries(s, campaign)

    async with SessionLocal() as s:
        sr_count = await s.scalar(
            select(func.count(SearchResult.id))
            .join(Query, SearchResult.query_id == Query.id)
            .where(Query.campaign_id == cid)
        )
        searched_q_count = await s.scalar(
            select(func.count(Query.id)).where(Query.campaign_id == cid, Query.status == "searched")
        )
        campaign = await s.get(Campaign, cid)

    assert searched == 3, f"expected 3 queries searched, got {searched}"
    assert searched_q_count == 3
    # Each query asks Serper for 10 results. Real-world we usually get 8-10. >=15 is a safe floor.
    assert sr_count >= 15, f"expected >=15 search_results, got {sr_count}"
    assert campaign.serper_credits_used == 3
    assert campaign.status != "budget_exceeded"


async def test_serper_search_is_idempotent_on_rerun():
    cid = await _seed_campaign_with_queries(SAMPLE_QUERIES[:2])

    # First run — real Serper.
    async with SessionLocal() as s:
        campaign = await s.get(Campaign, cid)
        await search_pending_queries(s, campaign)

    async with SessionLocal() as s:
        first_sr = await s.scalar(
            select(func.count(SearchResult.id))
            .join(Query, SearchResult.query_id == Query.id)
            .where(Query.campaign_id == cid)
        )
        first_credits = (await s.get(Campaign, cid)).serper_credits_used

    # Second run — should be a no-op (no pending queries left).
    async with SessionLocal() as s:
        campaign = await s.get(Campaign, cid)
        searched_again = await search_pending_queries(s, campaign)

    async with SessionLocal() as s:
        second_sr = await s.scalar(
            select(func.count(SearchResult.id))
            .join(Query, SearchResult.query_id == Query.id)
            .where(Query.campaign_id == cid)
        )
        second_credits = (await s.get(Campaign, cid)).serper_credits_used

    assert searched_again == 0, "second run must search 0 queries"
    assert second_sr == first_sr, "second run must add 0 search_results"
    assert second_credits == first_credits, "second run must spend 0 credits"


async def test_serper_budget_guard_halts_run():
    # Budget=1: only the first query should burn its credit; the rest stay pending.
    cid = await _seed_campaign_with_queries(SAMPLE_QUERIES, max_credits=1)

    async with SessionLocal() as s:
        campaign = await s.get(Campaign, cid)
        await search_pending_queries(s, campaign)

    async with SessionLocal() as s:
        campaign = await s.get(Campaign, cid)
        searched_qs = await s.scalar(
            select(func.count(Query.id)).where(Query.campaign_id == cid, Query.status == "searched")
        )
        pending_qs = await s.scalar(
            select(func.count(Query.id)).where(Query.campaign_id == cid, Query.status == "pending")
        )

    assert campaign.serper_credits_used <= 1
    assert searched_qs <= 1, f"only <=1 query should be searched on budget=1, got {searched_qs}"
    assert pending_qs >= len(SAMPLE_QUERIES) - 1
    assert campaign.status == "budget_exceeded"
