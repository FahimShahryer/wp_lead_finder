"""Discord stage 1 — query generation tests.

Mirrors the WhatsApp test shape but verifies the Discord anchor + Discord
snowball wrapper. Pure unit tests on the helpers + DB-only snowball tests
(no live OpenAI / Discord API needed).
"""
import os

import pytest
from sqlalchemy import select

from src.db.models import Campaign, Lead, Query
from src.db.session import SessionLocal
from src.pipeline.discord.queries import (
    ANCHOR as DISCORD_ANCHOR,
    snowball_from_verified_names,
)
from src.pipeline.shared.query_builders import build_queries


# ---------- Pure unit tests — no API keys needed ----------


def test_discord_anchor_is_discord_gg():
    assert DISCORD_ANCHOR == "discord.gg"


def test_build_queries_with_discord_anchor():
    pairs = build_queries(
        terms=["AI agency"],
        platforms=["reddit", "meetup"],
        negatives=["india"],
        target_count=10,
        anchor=DISCORD_ANCHOR,
    )
    assert len(pairs) > 0
    queries = [q for q, _ in pairs]
    # Every query must carry the discord.gg anchor.
    assert all("discord.gg" in q for q in queries), queries
    # Anchor unquoted (Serper-side block).
    assert not any('"discord.gg"' in q for q in queries)
    # No WhatsApp anchor cross-leak.
    assert not any("chat.whatsapp.com" in q for q in queries)
    # Term still quoted, negatives still applied.
    assert all('"AI agency"' in q for q in queries)
    assert all("-india" in q.lower() for q in queries)


# ---------- Snowball — DB-only ----------


async def _seed_campaign_with_enriched_leads(
    *,
    name: str = "step3-discord-snowball",
    verified_names: list[str] | None = None,
    scores: list[int] | None = None,
) -> int:
    verified_names = verified_names or []
    scores = scores or [80] * len(verified_names)
    async with SessionLocal() as s:
        c = Campaign(
            name=name,
            industries=["AI agency"],
            locations=["US"],
            negative_locations=["india"],
            platforms=["reddit", "meetup", "eventbrite"],
            platform="discord",
        )
        s.add(c)
        await s.flush()
        for i, (gn, score) in enumerate(zip(verified_names, scores)):
            s.add(
                Lead(
                    campaign_id=c.id,
                    invite_id=f"discord_invite_{i}",
                    source_url=f"https://example.com/post-{i}",
                    verified_group_name=gn,
                    total_score=score,
                )
            )
        await s.commit()
        return c.id


async def test_discord_snowball_returns_zero_when_no_enriched_leads():
    cid = await _seed_campaign_with_enriched_leads(verified_names=[])
    new_count = await snowball_from_verified_names(cid)
    assert new_count == 0


async def test_discord_snowball_seeds_queries_with_discord_anchor():
    cid = await _seed_campaign_with_enriched_leads(
        verified_names=[
            "AI Marketing Agency Server",
            "Indie Hackers Discord",
        ],
        scores=[90, 85],
    )

    new_count = await snowball_from_verified_names(cid)
    assert new_count > 0

    async with SessionLocal() as s:
        rows = list(
            (await s.execute(select(Query).where(Query.campaign_id == cid))).scalars()
        )

    queries = [r.query_text for r in rows]
    # Every snowball query should anchor on discord.gg, not chat.whatsapp.com.
    assert all("discord.gg" in q for q in queries), queries
    assert not any("chat.whatsapp.com" in q for q in queries)
    # Verified server names land as quoted seeds.
    assert any('"AI Marketing Agency Server"' in q for q in queries)
    assert any('"Indie Hackers Discord"' in q for q in queries)
    # Negative still applied.
    assert all("-india" in q.lower() for q in queries)


# ---------- Live OpenAI integration — gated ----------

live_test = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set; skipping live Discord query-gen test",
)


@live_test
async def test_discord_generate_queries_for_real_icp():
    from src.pipeline.discord.queries import generate_queries

    async with SessionLocal() as session:
        campaign = Campaign(
            name="step3-discord-marketing",
            industries=["Marketing agency owners"],
            locations=["US", "UK"],
            negative_locations=["India", "Indian"],
            platforms=["reddit", "meetup", "eventbrite", "web"],
            platform="discord",
        )
        session.add(campaign)
        await session.commit()
        rows = await generate_queries(session, campaign)

        print(f"\n--- {len(rows)} discord queries ---")
        for r in rows[:10]:
            print(f"  [{r.source_platform:6}] {r.query_text}")
        print("  …\n--- end ---")

        assert len(rows) >= 20
        # All queries anchor on discord.gg, NEVER on chat.whatsapp.com.
        anchored = sum(1 for r in rows if "discord.gg" in r.query_text)
        assert anchored / len(rows) >= 0.70, f"only {anchored}/{len(rows)} anchored"
        assert not any("chat.whatsapp.com" in r.query_text for r in rows)
