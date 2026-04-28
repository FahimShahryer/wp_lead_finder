import asyncio
import logging
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import func, select, update

from src.clients.openai_client import QUERY_GEN_MODEL, get_openai_client
from src.db.models import Campaign, Lead
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

BATCH_SIZE = 10
LLM_CONCURRENCY = 5
# Hard-cap each lead's source_text in the prompt so a giant scraped page doesn't
# blow the context window. 500 chars ≈ 100 tokens — plenty for industry/geo signal.
MAX_CONTEXT_CHARS = 500

# Weights used to roll the three sub-scores into a single rank-able total.
# Relevance dominates (right industry > right geo > engagement).
W_RELEVANCE = 0.5
W_GEO_FIT = 0.3
W_ENGAGEMENT = 0.2


class LeadScoreItem(BaseModel):
    invite_id: str = Field(description="The lead's invite_id, copied verbatim from the prompt")
    relevance: int = Field(ge=0, le=100, description="0-100, industry match")
    geo_fit: int = Field(ge=0, le=100, description="0-100, geography match; <=10 if a negative-location keyword is present")
    engagement: int = Field(ge=0, le=100, description="0-100, perceived activity/quality of the group")


class LeadScoreBatch(BaseModel):
    scores: list[LeadScoreItem]


SYSTEM_PROMPT = """\
You are an expert at scoring B2B WhatsApp-group leads against an Ideal Customer Profile (ICP).

For each lead, output three integer scores in [0, 100]:
  - relevance:   how well the group matches the TARGET INDUSTRIES.
  - geo_fit:     how well the audience matches the TARGET LOCATIONS.
                 STRICT RULE: if the lead's text mentions any of the EXCLUDED
                 locations/keywords, geo_fit MUST be ≤ 10.
  - engagement:  how active / serious / specific the group sounds, based on tone
                 and detail in the surrounding text.

Be ruthless. If the lead is clearly off-industry, give relevance < 25.
If the lead is clearly off-geo, give geo_fit < 25.
Don't pad scores; "average" leads should land 30-60 across the board.

Return one entry per input lead, keyed by invite_id (copied verbatim from the input).
"""


def _format_lead_block(leads: list[Lead]) -> str:
    parts: list[str] = []
    for ld in leads:
        ctx = (ld.source_text or "").strip().replace("\n", " ")
        if len(ctx) > MAX_CONTEXT_CHARS:
            ctx = ctx[:MAX_CONTEXT_CHARS] + "…"
        parts.append(f"[{ld.invite_id}] {ctx}")
    return "\n\n".join(parts)


def _build_user_msg(campaign: Campaign, leads: list[Lead]) -> str:
    return (
        f"Target industries: {', '.join(campaign.industries) or '(none)'}\n"
        f"Target locations: {', '.join(campaign.locations) or '(any)'}\n"
        f"EXCLUDE locations/keywords: {', '.join(campaign.negative_locations) or '(none)'}\n\n"
        f"Score these leads:\n\n{_format_lead_block(leads)}"
    )


def _total(item: LeadScoreItem) -> int:
    return round(
        item.relevance * W_RELEVANCE
        + item.geo_fit * W_GEO_FIT
        + item.engagement * W_ENGAGEMENT
    )


async def _score_batch(campaign: Campaign, batch: list[Lead]) -> list[LeadScoreItem]:
    client = get_openai_client()
    completion = await client.chat.completions.parse(
        model=QUERY_GEN_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_msg(campaign, batch)},
        ],
        response_format=LeadScoreBatch,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        refusal = completion.choices[0].message.refusal
        raise RuntimeError(f"OpenAI did not parse scoring response (refusal={refusal!r})")
    return parsed.scores


MAX_ATTEMPTS = 3


async def _score_one_pass(campaign: Campaign, leads: list[Lead]) -> int:
    """Score `leads` in batches; persist results. Returns the count newly scored."""
    batches = [leads[i : i + BATCH_SIZE] for i in range(0, len(leads), BATCH_SIZE)]
    sem = asyncio.Semaphore(LLM_CONCURRENCY)

    async def _run_batch(b: list[Lead]) -> list[LeadScoreItem]:
        async with sem:
            try:
                return await _score_batch(campaign, b)
            except Exception as e:
                logger.exception("scoring batch failed (size=%d): %s", len(b), e)
                return []

    batch_results = await asyncio.gather(*[_run_batch(b) for b in batches])
    flat = {item.invite_id: item for batch in batch_results for item in batch}
    if not flat:
        return 0

    now = datetime.now(timezone.utc)
    scored = 0
    async with SessionLocal() as s:
        for ld in leads:
            item = flat.get(ld.invite_id)
            if item is None:
                continue
            await s.execute(
                update(Lead)
                .where(Lead.id == ld.id)
                .values(
                    relevance=item.relevance,
                    geo_fit=item.geo_fit,
                    engagement=item.engagement,
                    total_score=_total(item),
                    last_scored_at=now,
                )
            )
            scored += 1
        await s.commit()
    return scored


async def score_for_campaign(campaign_id: int) -> dict[str, int]:
    """Stage 6: score every unscored lead for a campaign.

    Idempotent: only `last_scored_at IS NULL` rows are picked up.
    Robust: gpt-4o-mini occasionally returns empty `scores` arrays under
    structured-output mode. We retry up to MAX_ATTEMPTS over the still-unscored
    rows; each retry re-queries for `last_scored_at IS NULL` so already-scored
    leads from a partially-successful prior pass aren't redone.
    """
    async with SessionLocal() as s:
        campaign = await s.get(Campaign, campaign_id)
        if campaign is None:
            raise ValueError(f"campaign {campaign_id} not found")

    total_scored = 0
    last_remaining = -1

    for attempt in range(MAX_ATTEMPTS):
        async with SessionLocal() as s:
            leads = list(
                (
                    await s.execute(
                        select(Lead).where(
                            Lead.campaign_id == campaign_id,
                            Lead.last_scored_at.is_(None),
                        )
                    )
                ).scalars()
            )

        if not leads:
            break

        if len(leads) == last_remaining:
            # Made no progress on the last attempt — bail before burning more tokens.
            logger.warning(
                "stage 6: %d leads still unscored after %d attempts for campaign %s",
                len(leads), attempt, campaign_id,
            )
            break
        last_remaining = len(leads)

        scored_this_pass = await _score_one_pass(campaign, leads)
        total_scored += scored_this_pass
        logger.info(
            "stage 6 attempt %d for campaign %s: scored %d of %d",
            attempt + 1, campaign_id, scored_this_pass, len(leads),
        )
        if scored_this_pass == len(leads):
            break  # all done

    async with SessionLocal() as s:
        still_unscored = await s.scalar(
            select(func.count(Lead.id)).where(
                Lead.campaign_id == campaign_id,
                Lead.last_scored_at.is_(None),
            )
        )
    counts = {"scored": total_scored, "missing": still_unscored or 0, "skipped": 0}
    logger.info("stage 6 done for campaign %s: %s", campaign_id, counts)
    return counts
