import os

import pytest
from sqlalchemy import select

from src.db.models import Campaign, Lead
from src.db.session import SessionLocal
from src.pipeline.stage6_score import score_for_campaign

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set in container env; skipping live OpenAI test",
)


# Three groups of leads, each tagged with the expected category so we can assert.
# (invite_id, source_text, expected_bucket)
SEED_LEADS: list[tuple[str, str, str]] = [
    # FIT — right industry + right geo
    (
        "fit_us_ai_1",
        "AI Automation Agency Owners group — share invites here, US-based founders building "
        "n8n + GPT pipelines. join chat.whatsapp.com/fit_us_ai_1",
        "fit",
    ),
    (
        "fit_uk_marketing_2",
        "Marketing agency founders — UK chapter — networking + lead-gen tactics. "
        "weekly calls, paid masterminds. chat.whatsapp.com/fit_uk_marketing_2",
        "fit",
    ),
    (
        "fit_dubai_ai_3",
        "Dubai AI agency network — local meetups, paid clients in MENA, founders only. "
        "chat.whatsapp.com/fit_dubai_ai_3",
        "fit",
    ),
    # MISFIT_GEO — right industry but EXCLUDED location (India)
    (
        "misfit_india_1",
        "Indian AI agency owners group — Bangalore + Mumbai founders, share leads. "
        "chat.whatsapp.com/misfit_india_1",
        "misfit_geo",
    ),
    (
        "misfit_india_2",
        "WhatsApp group for Indian SaaS founders, mostly from Pune and Hyderabad. "
        "chat.whatsapp.com/misfit_india_2",
        "misfit_geo",
    ),
    # MISFIT_INDUSTRY — wrong industry entirely
    (
        "misfit_food_1",
        "Home cooking recipes group — share photos of meals, US suburban moms. "
        "chat.whatsapp.com/misfit_food_1",
        "misfit_industry",
    ),
    (
        "misfit_crypto_1",
        "Crypto day-traders pump signals — leveraged trades, anyone welcome from anywhere. "
        "chat.whatsapp.com/misfit_crypto_1",
        "misfit_industry",
    ),
    # AMBIGUOUS — vague, should land in the middle
    (
        "amb_1",
        "Tech entrepreneurs networking, mostly remote — chat.whatsapp.com/amb_1",
        "ambiguous",
    ),
    (
        "amb_2",
        "Marketing professionals globally — chat.whatsapp.com/amb_2",
        "ambiguous",
    ),
    (
        "amb_3",
        "AI builders general chat — chat.whatsapp.com/amb_3",
        "ambiguous",
    ),
]


async def _seed_campaign_with_leads() -> int:
    async with SessionLocal() as s:
        c = Campaign(
            name="step9-scoring",
            industries=["AI agency owners", "Marketing agency owners"],
            locations=["US", "UK", "Dubai"],
            negative_locations=["India", "Indian"],
            platforms=["reddit", "web"],
        )
        s.add(c)
        await s.flush()

        for invite_id, source_text, _bucket in SEED_LEADS:
            s.add(
                Lead(
                    invite_id=invite_id,
                    source_url=f"https://example.com/{invite_id}",
                    source_text=source_text,
                    campaign_id=c.id,
                )
            )
        await s.commit()
        return c.id


def _by_invite(leads: list[Lead]) -> dict[str, Lead]:
    return {l.invite_id: l for l in leads}


async def test_scoring_distinguishes_fit_misfit_and_negative_geo():
    cid = await _seed_campaign_with_leads()

    counts = await score_for_campaign(cid)
    assert counts["scored"] == len(SEED_LEADS), counts

    async with SessionLocal() as s:
        leads = list(
            (await s.execute(select(Lead).where(Lead.campaign_id == cid))).scalars()
        )
    by_id = _by_invite(leads)

    # Print so a human can sanity-check distribution
    print("\n--- step 9 scoring distribution ---")
    for invite_id, _txt, bucket in SEED_LEADS:
        l = by_id[invite_id]
        print(
            f"  [{bucket:18}] {invite_id:24} "
            f"rel={l.relevance:>3} geo={l.geo_fit:>3} eng={l.engagement:>3} "
            f"total={l.total_score:>3}"
        )
    print("--- end ---\n")

    # Hard gate assertions per the plan
    fit_ids = [iid for (iid, _, b) in SEED_LEADS if b == "fit"]
    geo_misfit_ids = [iid for (iid, _, b) in SEED_LEADS if b == "misfit_geo"]
    industry_misfit_ids = [iid for (iid, _, b) in SEED_LEADS if b == "misfit_industry"]

    # Fit leads: at least 2 of 3 must score > 60 total (allow one borderline)
    fit_totals = [by_id[i].total_score for i in fit_ids]
    above_60 = sum(1 for t in fit_totals if t > 60)
    assert above_60 >= 2, f"expected >=2 fit-leads above 60, got totals {fit_totals}"

    # Negative-location penalty: ALL India leads MUST have geo_fit <= 20.
    # The system prompt's "STRICT RULE" demands this — if it fails, the prompt is broken.
    for i in geo_misfit_ids:
        assert by_id[i].geo_fit <= 20, (
            f"negative-geo penalty failed for {i}: geo_fit={by_id[i].geo_fit}"
        )

    # Wrong-industry leads: relevance must be < 35 for at least one of them.
    # (Crypto + cooking are clearly not "AI/marketing agency owners".)
    industry_relevances = [by_id[i].relevance for i in industry_misfit_ids]
    assert min(industry_relevances) < 35, (
        f"expected at least one misfit-industry relevance < 35, got {industry_relevances}"
    )

    # last_scored_at must be set on every lead.
    assert all(l.last_scored_at is not None for l in leads)


async def test_scoring_is_idempotent_on_rerun():
    cid = await _seed_campaign_with_leads()
    first = await score_for_campaign(cid)
    second = await score_for_campaign(cid)

    assert first["scored"] == len(SEED_LEADS)
    # Second run: nothing eligible → all counters zero.
    assert second["scored"] == 0
    assert second["clamped"] == 0
    assert second["missing"] == 0


def test_source_bonus_curve():
    """Source-recurrence bonus shape: 1 → 0, sqrt curve, capped at 20."""
    from src.pipeline.stage6_score import _source_bonus

    # 1 source = baseline, no boost.
    assert _source_bonus(1) == 0
    assert _source_bonus(0) == 0
    assert _source_bonus(None) == 0
    # 2 sources: round(5 * sqrt(1)) = 5
    assert _source_bonus(2) == 5
    # 5 sources: round(5 * sqrt(4)) = 10
    assert _source_bonus(5) == 10
    # 17 sources: round(5 * sqrt(16)) = 20 (boundary)
    assert _source_bonus(17) == 20
    # Large counts cap at 20.
    assert _source_bonus(100) == 20
    assert _source_bonus(10_000) == 20


def test_total_score_applies_source_bonus_and_caps_at_100():
    """_total combines the LLM-weighted score and the source-recurrence bonus,
    clipping to 100 so a near-max lead with many sources doesn't overflow."""
    from src.pipeline.stage6_score import LeadScoreItem, _total

    item = LeadScoreItem(invite_id="x", relevance=80, geo_fit=80, engagement=80)
    # base = 0.5*80 + 0.3*80 + 0.2*80 = 80
    assert _total(item, source_count=1) == 80
    # base 80 + bonus 5 (2 sources) = 85
    assert _total(item, source_count=2) == 85

    near_max = LeadScoreItem(invite_id="y", relevance=100, geo_fit=100, engagement=100)
    # base 100 + bonus 10 (5 sources) → clipped to 100
    assert _total(near_max, source_count=5) == 100

    junk = LeadScoreItem(invite_id="z", relevance=0, geo_fit=0, engagement=0)
    # Even with many sources, base 0 + bonus 20 = 20. Junk stays junk.
    assert _total(junk, source_count=100) == 20


async def test_scoring_picks_up_leads_re_validated_after_last_scoring():
    """When WA enrichment runs after a lead was scored on noisy snippet text,
    a re-score with the verified_group_name should be triggered. This is the
    'enrichment data is ground truth' lever — verified group name is much
    higher-signal than the 200-char snippet around the invite."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update

    cid = await _seed_campaign_with_leads()
    first = await score_for_campaign(cid)
    assert first["scored"] == len(SEED_LEADS)

    # Pick one ambiguous lead and "enrich" it with a verified group name that
    # CLEARLY puts it in the target industry. The re-score should bump its
    # relevance materially upward.
    target_invite = "amb_3"  # was "AI builders general chat" — ambiguous
    async with SessionLocal() as s:
        before = (
            await s.execute(select(Lead).where(Lead.invite_id == target_invite))
        ).scalar_one()
        before_relevance = before.relevance
        before_scored_at = before.last_scored_at

        # Simulate enrichment landing AFTER the prior scoring: bump
        # last_validated_at past last_scored_at.
        await s.execute(
            update(Lead)
            .where(Lead.invite_id == target_invite)
            .values(
                verified_group_name="AI Marketing Agency Founders Network — US/UK/Dubai",
                verified_group_description=(
                    "Active community of marketing agency owners running AI "
                    "automation services for clients. Weekly mastermind calls."
                ),
                last_validated_at=before_scored_at + timedelta(seconds=1),
            )
        )
        await s.commit()

    second = await score_for_campaign(cid)
    # Only the one re-validated lead should be re-scored.
    assert second["scored"] == 1, (
        f"expected exactly 1 lead re-scored after enrichment, got {second}"
    )

    async with SessionLocal() as s:
        after = (
            await s.execute(select(Lead).where(Lead.invite_id == target_invite))
        ).scalar_one()

    assert after.last_scored_at > before_scored_at, "re-score must bump last_scored_at"
    # Verified group name + description should push relevance up significantly.
    # We assert >=70 because both fields explicitly state "marketing agency".
    assert after.relevance >= 70, (
        f"relevance should jump after enrichment with on-industry verified name, "
        f"before={before_relevance} after={after.relevance}"
    )
