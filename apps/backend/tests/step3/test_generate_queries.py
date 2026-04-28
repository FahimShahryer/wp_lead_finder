import os

import pytest

from src.db.models import Campaign
from src.db.session import SessionLocal
from src.pipeline.stage1_queries import generate_queries

# Live OpenAI test — gated on key being present in the running container's env.
pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set in container env; skipping live OpenAI test",
)


async def test_generate_queries_for_real_icp():
    async with SessionLocal() as session:
        campaign = Campaign(
            name="step3-agency-owners",
            industries=["AI agency owners", "Marketing agency owners"],
            locations=["US", "UK", "Dubai"],
            negative_locations=["India", "Indian"],
            platforms=["reddit", "web"],
        )
        session.add(campaign)
        await session.commit()

        rows = await generate_queries(session, campaign)

        # Print so a human can eyeball the output for "real prospector" feel.
        print(f"\n--- {len(rows)} generated queries ---")
        for r in rows:
            print(f"  [{r.source_platform:6}] {r.query_text}")
        print("--- end ---\n")

        # Hard gate assertions
        assert len(rows) >= 20, f"expected >=20 queries, got {len(rows)}"

        reddit_count = sum(1 for r in rows if "site:reddit.com" in r.query_text.lower())
        assert reddit_count >= 5, (
            f"expected >=5 site:reddit.com queries, got {reddit_count}"
        )

        negative_count = sum(
            1
            for r in rows
            if "-india" in r.query_text.lower() or "-indian" in r.query_text.lower()
        )
        assert negative_count >= 3, (
            f"expected >=3 queries with -india/-indian, got {negative_count}"
        )

        too_long = [r.query_text for r in rows if len(r.query_text) > 200]
        assert not too_long, f"queries over 200 chars: {too_long}"

        texts = [r.query_text.lower().strip() for r in rows]
        assert len(texts) == len(set(texts)), "duplicate queries detected"


async def test_generate_queries_emits_literal_phrase_queries_for_hard_firewall_platforms():
    """When campaign.platforms includes facebook/linkedin, the prompt instructs
    the model to emit literal-phrase `"chat.whatsapp.com" ... site:X` queries
    that hunt for invite links Google has already indexed in snippets."""
    async with SessionLocal() as session:
        campaign = Campaign(
            name="step3-hard-firewall",
            industries=["AI agency owners"],
            locations=["US"],
            negative_locations=[],
            platforms=["reddit", "web", "facebook", "linkedin"],
        )
        session.add(campaign)
        await session.commit()

        rows = await generate_queries(session, campaign)

        # Must include at least one literal-phrase query per hard-firewall platform.
        # The literal `"chat.whatsapp.com"` (in double quotes) is the signature.
        fb_hits = [
            r for r in rows
            if "site:facebook.com" in r.query_text.lower()
            and '"chat.whatsapp.com"' in r.query_text.lower()
        ]
        li_hits = [
            r for r in rows
            if "site:linkedin.com" in r.query_text.lower()
            and '"chat.whatsapp.com"' in r.query_text.lower()
        ]

        # At least 2 each — the prompt asks for 3-5, but allow some leeway for
        # LLM creativity (it may emit a couple as raw `chat.whatsapp.com` and
        # we filter on the literal-phrase form here).
        assert len(fb_hits) >= 2, (
            f"expected >=2 literal-phrase facebook queries, got {len(fb_hits)} "
            f"out of {len(rows)} total queries"
        )
        assert len(li_hits) >= 2, (
            f"expected >=2 literal-phrase linkedin queries, got {len(li_hits)}"
        )


async def test_generate_queries_is_idempotent():
    """Re-running stage 1 on the same campaign must NOT call OpenAI again
    and must return the same rows."""
    async with SessionLocal() as session:
        campaign = Campaign(
            name="step3-idempotency",
            industries=["SaaS founders"],
            locations=["US"],
            negative_locations=[],
            platforms=["reddit"],
        )
        session.add(campaign)
        await session.commit()

        first = await generate_queries(session, campaign)
        first_ids = sorted(q.id for q in first)
        first_count = len(first)

        # Second run — should be a no-op LLM-wise, just return existing.
        second = await generate_queries(session, campaign)
        second_ids = sorted(q.id for q in second)

        assert second_ids == first_ids, "second run should return the same query rows"
        assert len(second) == first_count
