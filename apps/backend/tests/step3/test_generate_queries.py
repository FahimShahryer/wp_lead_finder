"""Stage 1 — query generation tests.

Architecture: 1A (LLM term expansion) + 1B (Python combinator).
The 1A call is gated on OPENAI_API_KEY; the 1B logic has its own pure
unit tests below that don't require any API keys.
"""
import os

import pytest

from src.db.models import Campaign
from src.db.session import SessionLocal
from src.pipeline.shared.query_builders import (
    ALLOWED_QUERY_PLATFORMS,
    _format_negatives,
    _resolve_query_platforms,
    build_queries,
)
from src.pipeline.whatsapp.queries import generate_queries


# ---------- Pure unit tests for stage 1B (no API keys required) ----------


def test_resolve_query_platforms_returns_only_allowed():
    # Legacy values like "facebook" / "web" silently dropped.
    assert _resolve_query_platforms(["reddit", "facebook", "web"]) == ["reddit"]
    assert _resolve_query_platforms(["meetup", "linkedin"]) == ["meetup"]


def test_resolve_query_platforms_falls_back_when_empty():
    # Empty / all-disallowed inputs default to all 3 allowed platforms.
    assert _resolve_query_platforms([]) == list(ALLOWED_QUERY_PLATFORMS)
    assert _resolve_query_platforms(["facebook", "linkedin"]) == list(ALLOWED_QUERY_PLATFORMS)
    assert _resolve_query_platforms(None) == list(ALLOWED_QUERY_PLATFORMS)


def test_format_negatives_renders_clean_tokens():
    assert _format_negatives(["India", "Indian"]) == "-india -indian"
    # Drops multi-word entries (Google's `-` only takes single tokens) and dupes.
    assert _format_negatives(["india", "Indian Subcontinent", "India"]) == "-india"
    # Empty input → empty string.
    assert _format_negatives([]) == ""
    assert _format_negatives(None) == ""


def test_build_queries_produces_anchor_on_t1_t2():
    # Every query in T1 + T2 buckets MUST contain the chat.whatsapp.com anchor
    # (unquoted — Serper blocks the exact-phrase form). T3 queries (looser) are
    # 10% of the budget so up to ~10% of total queries may not have the anchor.
    pairs = build_queries(
        terms=["marketing agency", "agency owners", "agency founders"],
        platforms=["reddit", "meetup", "eventbrite"],
        negatives=["india", "indian"],
        target_count=30,
        anchor="chat.whatsapp.com",
    )
    queries = [q for q, _ in pairs]
    assert len(queries) > 0
    anchored = sum(1 for q in queries if "chat.whatsapp.com" in q)
    # ≥ 70% should have the anchor (T1 55% + T2 35% = 90% target; allow slack).
    assert anchored / len(queries) >= 0.70, (
        f"only {anchored}/{len(queries)} queries had the chat.whatsapp.com anchor"
    )
    # And the anchor must NOT be wrapped in double quotes (Serper-side block).
    assert not any('"chat.whatsapp.com"' in q for q in queries), (
        "anchor must be unquoted to avoid Serper 'Query not allowed' rejection"
    )


def test_build_queries_quotes_industry_terms():
    pairs = build_queries(
        terms=["marketing agency"],
        platforms=["reddit"],
        negatives=[],
        target_count=10,
        anchor="chat.whatsapp.com",
    )
    # Every query MUST quote the term — `"marketing agency"` not `marketing agency`.
    for q, _ in pairs:
        assert '"marketing agency"' in q, f"term not quoted in {q!r}"


def test_build_queries_applies_negatives_consistently():
    pairs = build_queries(
        terms=["saas founders"],
        platforms=["reddit", "meetup"],
        negatives=["india", "indian"],
        target_count=20,
        anchor="chat.whatsapp.com",
    )
    # Every query should include both negatives.
    for q, _ in pairs:
        assert "-india" in q.lower(), f"missing -india in {q!r}"
        assert "-indian" in q.lower(), f"missing -indian in {q!r}"


def test_build_queries_dedupes_near_misses():
    pairs = build_queries(
        terms=["marketing agency", "agency marketing"],  # same tokens, different order
        platforms=["reddit"],
        negatives=[],
        target_count=20,
        anchor="chat.whatsapp.com",
    )
    # Dedup uses sorted-token normalization, so word-order swaps collapse.
    queries = [q for q, _ in pairs]
    assert len(queries) == len(set(queries)), "duplicate queries detected"


def test_build_queries_caps_to_target_count():
    pairs = build_queries(
        terms=["x", "y"],
        platforms=["reddit", "meetup", "eventbrite"],
        negatives=[],
        target_count=5,
        anchor="chat.whatsapp.com",
    )
    assert len(pairs) <= 5


def test_build_queries_under_200_chars():
    long_term = "very specific niche audience phrase for testing"
    pairs = build_queries(
        terms=[long_term],
        platforms=["reddit", "meetup", "eventbrite"],
        negatives=["india", "indian", "pakistan"],
        target_count=20,
        anchor="chat.whatsapp.com",
    )
    for q, _ in pairs:
        assert len(q) <= 200, f"query too long ({len(q)} chars): {q}"


def test_build_queries_marks_reddit_source_correctly():
    pairs = build_queries(
        terms=["saas"],
        platforms=["reddit", "meetup"],
        negatives=[],
        target_count=20,
        anchor="chat.whatsapp.com",
    )
    # Any query containing site:reddit.com must have source_platform='reddit'.
    for q, src in pairs:
        if "site:reddit.com" in q:
            assert src == "reddit", f"site:reddit.com query has source={src}: {q}"
        else:
            assert src == "web", f"non-reddit query has source={src}: {q}"


# ---------- Live OpenAI integration tests for stage 1A ----------

live_test = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set in container env; skipping live OpenAI test",
)


@live_test
async def test_generate_queries_for_real_icp():
    async with SessionLocal() as session:
        campaign = Campaign(
            name="step3-marketing-agency",
            industries=["Marketing agency owners"],
            locations=["US", "UK", "Dubai"],
            negative_locations=["India", "Indian"],
            platforms=["reddit", "meetup", "eventbrite", "web"],
        )
        session.add(campaign)
        await session.commit()

        rows = await generate_queries(session, campaign)

        # Print so a human can eyeball the output for "real prospector" feel.
        print(f"\n--- {len(rows)} generated queries ---")
        for r in rows:
            print(f"  [{r.source_platform:6}] {r.query_text}")
        print("--- end ---\n")

        # Hard gate: enough queries to fill a campaign.
        assert len(rows) >= 20, f"expected >=20 queries, got {len(rows)}"

        # Anchor: ≥70% of queries should carry the chat.whatsapp.com anchor
        # (unquoted — Serper blocks the exact-phrase form).
        anchored = sum(1 for r in rows if "chat.whatsapp.com" in r.query_text)
        assert anchored / len(rows) >= 0.70, (
            f"only {anchored}/{len(rows)} queries had the anchor (need ≥70%)"
        )

        # site:reddit.com queries must be present.
        reddit_count = sum(1 for r in rows if "site:reddit.com" in r.query_text.lower())
        assert reddit_count >= 5, f"expected >=5 site:reddit.com queries, got {reddit_count}"

        # site:meetup.com queries must be present (top-yield platform per data).
        meetup_count = sum(1 for r in rows if "site:meetup.com" in r.query_text.lower())
        assert meetup_count >= 3, f"expected >=3 site:meetup.com queries, got {meetup_count}"

        # Negatives applied broadly.
        negative_count = sum(
            1 for r in rows
            if "-india" in r.query_text.lower() or "-indian" in r.query_text.lower()
        )
        assert negative_count >= len(rows) * 0.7, (
            f"expected most queries to carry -india/-indian, got {negative_count}/{len(rows)}"
        )

        # No social-platform queries — those were dropped in the rewrite.
        assert not any("site:facebook.com" in r.query_text.lower() for r in rows)
        assert not any("site:linkedin.com" in r.query_text.lower() for r in rows)
        assert not any("site:twitter.com" in r.query_text.lower() for r in rows)
        assert not any("site:x.com" in r.query_text.lower() for r in rows)

        # Length budget.
        too_long = [r.query_text for r in rows if len(r.query_text) > 200]
        assert not too_long, f"queries over 200 chars: {too_long}"

        # No duplicates.
        texts = [r.query_text.lower().strip() for r in rows]
        assert len(texts) == len(set(texts)), "duplicate queries detected"


@live_test
async def test_generate_queries_is_idempotent():
    """Re-running stage 1 on the same campaign must NOT call OpenAI again
    and must return the same rows."""
    async with SessionLocal() as session:
        campaign = Campaign(
            name="step3-idempotency",
            industries=["SaaS"],
            locations=["US"],
            negative_locations=[],
            platforms=["reddit", "meetup", "eventbrite"],
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
