"""Slack stage 1 — query generation tests.

Mirrors the Discord test shape but verifies the Slack anchor + Slack
snowball wrapper.
"""
import os

import pytest
from sqlalchemy import select

from src.db.models import Campaign, Lead, Query
from src.db.session import SessionLocal
from src.pipeline.shared.query_builders import build_queries
from src.pipeline.slack.queries import (
    ANCHOR as SLACK_ANCHOR,
    snowball_from_verified_names,
)


# ---------- Pure unit tests ----------


def test_slack_anchor_is_join_slack_com():
    assert SLACK_ANCHOR == "join.slack.com"


def test_build_queries_with_slack_anchor():
    pairs = build_queries(
        terms=["marketing agency"],
        platforms=["reddit", "meetup"],
        negatives=["india"],
        target_count=10,
        anchor=SLACK_ANCHOR,
    )
    assert len(pairs) > 0
    queries = [q for q, _ in pairs]
    # Every query must carry the join.slack.com anchor.
    assert all("join.slack.com" in q for q in queries), queries
    # Anchor unquoted (Serper-side block).
    assert not any('"join.slack.com"' in q for q in queries)
    # No cross-platform anchor leak.
    assert not any("chat.whatsapp.com" in q for q in queries)
    assert not any("discord.gg" in q for q in queries)
    # Term still quoted, negatives still applied.
    assert all('"marketing agency"' in q for q in queries)
    assert all("-india" in q.lower() for q in queries)


# ---------- Snowball — DB-only ----------


async def _seed_campaign_with_enriched_leads(
    *,
    name: str = "step3-slack-snowball",
    verified_names: list[str] | None = None,
    scores: list[int] | None = None,
) -> int:
    verified_names = verified_names or []
    scores = scores or [80] * len(verified_names)
    async with SessionLocal() as s:
        c = Campaign(
            name=name,
            industries=["marketing agency"],
            locations=["US"],
            negative_locations=["india"],
            platforms=["reddit", "meetup", "eventbrite"],
            platform="slack",
        )
        s.add(c)
        await s.flush()
        for i, (gn, score) in enumerate(zip(verified_names, scores)):
            s.add(
                Lead(
                    campaign_id=c.id,
                    invite_id=f"workspace{i}/zt-token{i}-AAAAAAAA",
                    source_url=f"https://example.com/post-{i}",
                    verified_group_name=gn,
                    total_score=score,
                )
            )
        await s.commit()
        return c.id


async def test_slack_snowball_returns_zero_when_no_enriched_leads():
    cid = await _seed_campaign_with_enriched_leads(verified_names=[])
    new_count = await snowball_from_verified_names(cid)
    assert new_count == 0


async def test_slack_snowball_seeds_queries_with_slack_anchor():
    cid = await _seed_campaign_with_enriched_leads(
        verified_names=["Demand Curve", "RevOps Co-op"],
        scores=[92, 85],
    )

    new_count = await snowball_from_verified_names(cid)
    assert new_count > 0

    async with SessionLocal() as s:
        rows = list(
            (await s.execute(select(Query).where(Query.campaign_id == cid))).scalars()
        )

    queries = [r.query_text for r in rows]
    # Every snowball query anchors on join.slack.com.
    assert all("join.slack.com" in q for q in queries), queries
    # No cross-platform leak in the queries.
    assert not any("chat.whatsapp.com" in q for q in queries)
    assert not any("discord.gg" in q for q in queries)
    # Verified workspace names land as quoted seeds.
    assert any('"Demand Curve"' in q for q in queries)
    assert any('"RevOps Co-op"' in q for q in queries)
    # Negatives still applied.
    assert all("-india" in q.lower() for q in queries)


# ---------- Live OpenAI integration — gated ----------

live_test = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set; skipping live Slack query-gen test",
)


@live_test
async def test_slack_generate_queries_for_real_icp():
    from src.pipeline.slack.queries import generate_queries

    async with SessionLocal() as session:
        campaign = Campaign(
            name="step3-slack-marketing",
            industries=["Marketing agency owners"],
            locations=["US", "UK"],
            negative_locations=["India", "Indian"],
            platforms=["reddit", "meetup", "eventbrite", "web"],
            platform="slack",
        )
        session.add(campaign)
        await session.commit()
        rows = await generate_queries(session, campaign)

        print(f"\n--- {len(rows)} slack queries ---")
        for r in rows[:8]:
            print(f"  [{r.source_platform:6}] {r.query_text}")
        print("  …\n--- end ---")

        assert len(rows) >= 20
        anchored = sum(1 for r in rows if "join.slack.com" in r.query_text)
        assert anchored / len(rows) >= 0.70, f"only {anchored}/{len(rows)} anchored"
        # No cross-platform anchor leak.
        assert not any("chat.whatsapp.com" in r.query_text for r in rows)
        assert not any("discord.gg" in r.query_text for r in rows)
