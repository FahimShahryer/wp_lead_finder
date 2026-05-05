"""Snowball search — recycle verified group names back into stage 1B.

These tests are DB-only; no live OpenAI or Reddit needed. We seed leads with
verified_group_name values directly so the LLM term-expansion is bypassed.
"""
from sqlalchemy import select

from src.db.models import Campaign, Lead, Query
from src.db.session import SessionLocal
from src.pipeline.whatsapp.queries import snowball_from_verified_names


async def _seed_campaign_with_enriched_leads(
    *,
    name: str = "step3-snowball",
    verified_names: list[str] | None = None,
    scores: list[int] | None = None,
    industries: list[str] | None = None,
) -> int:
    industries = industries or ["marketing agency"]
    verified_names = verified_names or []
    scores = scores or [80] * len(verified_names)
    assert len(verified_names) == len(scores)

    async with SessionLocal() as s:
        c = Campaign(
            name=name,
            industries=industries,
            locations=["US"],
            negative_locations=["india", "indian"],
            platforms=["reddit", "meetup", "eventbrite"],
        )
        s.add(c)
        await s.flush()

        for i, (gn, score) in enumerate(zip(verified_names, scores)):
            s.add(
                Lead(
                    campaign_id=c.id,
                    invite_id=f"snowball_invite_{i}",
                    source_url=f"https://example.com/post-{i}",
                    verified_group_name=gn,
                    total_score=score,
                )
            )
        await s.commit()
        return c.id


async def test_snowball_returns_zero_when_no_enriched_leads():
    cid = await _seed_campaign_with_enriched_leads(verified_names=[])
    new_count = await snowball_from_verified_names(cid)
    assert new_count == 0


async def test_snowball_seeds_queries_from_verified_group_names():
    cid = await _seed_campaign_with_enriched_leads(
        verified_names=[
            "AI Marketing Agency Founders",
            "Digital Agency Owners Network",
            "Creative Agency Leaders",
        ],
        scores=[90, 85, 80],
    )

    new_count = await snowball_from_verified_names(cid)
    assert new_count > 0

    async with SessionLocal() as s:
        rows = list(
            (
                await s.execute(select(Query).where(Query.campaign_id == cid))
            ).scalars()
        )

    assert len(rows) == new_count
    queries = [r.query_text for r in rows]
    # Every snowball query must carry the chat.whatsapp.com anchor (T1+T2 only).
    # Anchor is unquoted to avoid Serper's exact-phrase block.
    assert all("chat.whatsapp.com" in q for q in queries), queries
    # At least one of each group name should appear quoted in the queries.
    assert any('"AI Marketing Agency Founders"' in q for q in queries)
    assert any('"Digital Agency Owners Network"' in q for q in queries)
    # All queries should still apply the campaign's negative-location filter.
    assert all("-india" in q.lower() for q in queries)


async def test_snowball_dedupes_against_existing_queries():
    """If a snowball-generated query happens to match one already in the
    queries table (e.g. an earlier campaign run produced an equivalent
    query), it must NOT be inserted again."""
    cid = await _seed_campaign_with_enriched_leads(
        verified_names=["AI Agency Founders Network"],
    )

    # First snowball run inserts everything.
    first_count = await snowball_from_verified_names(cid)
    assert first_count > 0

    # Second run must add zero — every query is already in the table.
    second_count = await snowball_from_verified_names(cid)
    assert second_count == 0


async def test_snowball_skips_overly_long_group_names():
    """Group names longer than MAX_GROUP_NAME_AS_TERM_LEN are dropped — they
    won't fit cleanly in a quoted query and tend to be SEO-junk anyway."""
    long_name = "Welcome to our incredibly comprehensive AI agency owners networking community of founders sharing tactics and resources daily"
    cid = await _seed_campaign_with_enriched_leads(
        verified_names=[long_name, "Short Useful Name"],
        scores=[95, 80],
    )

    await snowball_from_verified_names(cid)

    async with SessionLocal() as s:
        rows = list(
            (
                await s.execute(select(Query).where(Query.campaign_id == cid))
            ).scalars()
        )
    queries = [r.query_text for r in rows]
    # Short name must appear, long name must not.
    assert any('"Short Useful Name"' in q for q in queries)
    assert not any(long_name in q for q in queries)
