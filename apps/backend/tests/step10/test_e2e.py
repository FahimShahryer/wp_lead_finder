"""Step 10 e2e test: POST /campaigns → arq → worker → poll → ranked leads.

This test depends on the worker container running with the updated `run_campaign` task,
real keys for OpenAI / Serper / Firecrawl / Reddit, and external network. It is the
slowest test in the suite (3-8 minutes) and uses real credits — keep the campaign small.
"""

import asyncio
import os

import httpx
import pytest

from src.db.models import Campaign, Query, SearchResult
from src.db.session import SessionLocal
from src.pipeline.run_campaign import run_campaign

REQUIRED_KEYS = ("OPENAI_API_KEY", "SERPER_API_KEY", "FIRECRAWL_API_KEY", "REDDIT_CLIENT_ID")
pytestmark = pytest.mark.skipif(
    not all(os.getenv(k) for k in REQUIRED_KEYS),
    reason=f"missing one of {REQUIRED_KEYS}; skipping live e2e",
)

API_URL = os.getenv("API_URL", "http://localhost:8000")


SMALL_CAMPAIGN = {
    "name": "step10-e2e",
    "industries": ["AI agency owners"],
    "locations": ["US", "UK"],
    "negative_locations": ["India"],
    # Match the new default platforms (post stage-1 rewrite). reddit / meetup /
    # eventbrite each contribute their own site: queries; "web" is implicit
    # (T2 has no site: constraint anyway).
    "platforms": ["reddit", "meetup", "eventbrite", "web"],
    # Tight caps to keep the test cheap. Stage 1B's cartesian product over
    # ~6-10 LLM-expanded terms × 3 site:platforms × 2 templates produces
    # 25-50 queries; stage 2 halts at the Serper cap, 4b at the Firecrawl cap.
    "max_credits_serper": 8,
    "max_credits_firecrawl": 5,
}


async def _poll_until_terminal(cid: int, max_minutes: int = 8) -> dict:
    """Wait until status is terminal AND current_stage is None — i.e., the
    orchestrator has run _finalize. Just observing 'budget_exceeded' isn't
    enough since stages 5+6 still run after a budget halt."""
    deadline = asyncio.get_event_loop().time() + max_minutes * 60
    last_body: dict = {}
    async with httpx.AsyncClient(timeout=15) as client:
        while asyncio.get_event_loop().time() < deadline:
            r = await client.get(f"{API_URL}/campaigns/{cid}")
            r.raise_for_status()
            last_body = r.json()
            if (
                last_body["status"] in ("done", "budget_exceeded", "failed")
                and last_body["current_stage"] is None
            ):
                return last_body
            await asyncio.sleep(5)
    pytest.fail(f"campaign {cid} did not terminate within {max_minutes}min; last_body={last_body}")


async def test_e2e_post_campaign_runs_to_completion_via_worker():
    """Full pipeline via REST → arq → worker. Verifies:
      - POST /campaigns returns id with status='queued'
      - Worker picks the job up
      - Pipeline reaches a terminal status (done / budget_exceeded)
      - Counts in /campaigns/{id} match what's in the DB exactly
      - GET /campaigns/{id}/leads returns scored, ranked leads
    """
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{API_URL}/campaigns", json=SMALL_CAMPAIGN)
    assert r.status_code == 201, r.text
    body = r.json()
    cid = body["id"]
    assert body["status"] == "queued"

    final = await _poll_until_terminal(cid, max_minutes=8)
    assert final["status"] in ("done", "budget_exceeded"), final
    assert final["current_stage"] is None
    assert final["queries_count"] >= 20

    # ---- Cost ledger crosscheck: API counts must equal DB counts. ----
    async with SessionLocal() as s:
        from sqlalchemy import func, select

        db_queries = await s.scalar(
            select(func.count(Query.id)).where(Query.campaign_id == cid)
        )
        db_search_results = await s.scalar(
            select(func.count(SearchResult.id))
            .join(Query, SearchResult.query_id == Query.id)
            .where(Query.campaign_id == cid)
        )
    assert final["queries_count"] == db_queries
    assert final["search_results_count"] == db_search_results

    # Each Serper search costs exactly 1 credit; we get ~10 results per credit.
    # search_results count should be roughly serper_credits_used * 10 (allow slack).
    assert final["serper_credits_used"] <= final["max_credits_serper"]
    assert final["firecrawl_credits_used"] <= final["max_credits_firecrawl"]

    # ---- Leads endpoint: ranked + at least some scored ----
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{API_URL}/campaigns/{cid}/leads", params={"limit": 50})
    leads = r.json()

    # We may legitimately get 0 leads on a tight budget if every fetched page lacks
    # an invite. But on the AI/agency keyword we usually get a few.
    if leads:
        # Must be ordered by total_score DESC, with NULL scores last
        scores = [l["total_score"] for l in leads]
        non_null = [s for s in scores if s is not None]
        assert non_null == sorted(non_null, reverse=True), f"leads not score-sorted: {scores}"

        # only_scored filter should drop unscored ones
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{API_URL}/campaigns/{cid}/leads", params={"only_scored": True, "limit": 50}
            )
        scored = r.json()
        assert all(l["total_score"] is not None for l in scored)


async def test_run_campaign_is_idempotent_when_re_enqueued():
    """Direct call to the orchestrator twice — simulates "worker crashed mid-run,
    job got re-tried". Each stage is idempotent at the DB level; the second pass
    must not duplicate queries / search_results / leads or burn extra credits.
    """
    async with SessionLocal() as s:
        c = Campaign(
            name="step10-idempotent",
            industries=["SaaS founders"],
            locations=["US"],
            negative_locations=[],
            platforms=["reddit"],
            max_credits_serper=2,
            max_credits_firecrawl=2,
        )
        s.add(c)
        await s.commit()
        cid = c.id

    # First run
    await run_campaign(cid)

    async with SessionLocal() as s:
        first = await s.get(Campaign, cid)
        first_status = first.status
        first_serper = first.serper_credits_used
        first_firecrawl = first.firecrawl_credits_used
        from sqlalchemy import func, select

        first_queries = await s.scalar(
            select(func.count(Query.id)).where(Query.campaign_id == cid)
        )
        first_srs = await s.scalar(
            select(func.count(SearchResult.id))
            .join(Query, SearchResult.query_id == Query.id)
            .where(Query.campaign_id == cid)
        )

    # Re-run (terminal status guard returns early; nothing should change)
    await run_campaign(cid)

    async with SessionLocal() as s:
        second = await s.get(Campaign, cid)
        second_status = second.status
        second_serper = second.serper_credits_used
        second_firecrawl = second.firecrawl_credits_used
        second_queries = await s.scalar(
            select(func.count(Query.id)).where(Query.campaign_id == cid)
        )
        second_srs = await s.scalar(
            select(func.count(SearchResult.id))
            .join(Query, SearchResult.query_id == Query.id)
            .where(Query.campaign_id == cid)
        )

    assert second_status == first_status
    assert second_serper == first_serper
    assert second_firecrawl == first_firecrawl
    assert second_queries == first_queries
    assert second_srs == first_srs
