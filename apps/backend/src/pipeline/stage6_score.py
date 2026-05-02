import asyncio
import logging
import re
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update

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

Each campaign targets ONE narrow industry. Your job is binary at the extremes:
  - Is this group clearly about the target industry? → relevance high.
  - Is this group clearly about something else (crypto, dating, food,
    unrelated regions, generic "share your whatsapp" megathreads)? → relevance
    near zero. Do NOT pad these to 25-30; off-industry must be 0-10.

Field-weighting order (highest signal first):
  group_name > source_title > source_url > source_context

If group_name is present (post-enrichment), it is the GROUND TRUTH for what
the group is — weight it above every other field. A WhatsApp group whose own
name says "AI Marketing Agency Founders Network" is fully relevant for a
"marketing agencies" ICP regardless of how noisy the source page was.

source_title is the page where the invite was posted; for a Reddit thread it
is often the most precise pre-enrichment signal. source_url's path/subreddit
also carries strong signal (e.g. reddit.com/r/Entrepreneur tells you a lot
more than reddit.com/r/AskReddit).

Output three integer scores in [0, 100] per lead:
  - relevance:   how well the group matches the TARGET INDUSTRIES.
                 0-10 = clearly off-topic.
                 11-30 = adjacent / overlapping audience but not the target.
                 31-60 = plausibly fits but ambiguous evidence.
                 61-85 = clear fit with at least one strong on-industry signal.
                 86-100 = group_name or source_title explicitly states the
                          target industry.
  - geo_fit:     how well the audience matches the TARGET LOCATIONS.
                 STRICT RULE: if ANY field (group_name, title, url, context)
                 mentions any of the EXCLUDED locations/keywords, geo_fit
                 MUST be ≤ 10. No exceptions.
                 If locations are unspecified ("any"), default geo_fit to 50
                 unless an excluded keyword fires.
  - engagement:  how active / serious / specific the group sounds. If you
                 only have source_context to judge from (no verified name),
                 score conservatively (30-50 range) — engagement is
                 unreliable from scraped snippets alone.

Be ruthless. Padded scores are worse than wrong scores: they bury the truly
relevant leads in a sea of mid-tier noise.

Return one entry per input lead, keyed by invite_id (copied verbatim from the input).
"""


def _format_lead_block(leads: list[Lead]) -> str:
    """Lead presented to the scorer. Field order is by signal strength:
      1) verified_group_name — post-enrichment, what WhatsApp itself calls the
         group; highest-precision relevance signal when present.
      2) verified_group_description — rare but informative.
      3) source_url — the page where the invite was found. URL alone carries
         a lot of signal (subreddit slug, host, title-slug for Reddit threads).
      4) source_title — SERP/page title; tells the scorer what the host
         page is *about*, which is usually a much stronger industry/geo
         signal than the 200-char text window around the invite.
      5) source_context — the ±200-char excerpt from the surrounding text;
         can be noisy ("thanks bro!"), so the model relies on it last.
    """
    parts: list[str] = []
    for ld in leads:
        lines = [f"[{ld.invite_id}]"]
        if ld.verified_group_name:
            lines.append(f"  group_name: {ld.verified_group_name}")
        if ld.verified_group_description:
            lines.append(f"  group_description: {ld.verified_group_description}")
        if ld.source_url:
            lines.append(f"  source_url: {ld.source_url}")
        if ld.source_title:
            title = ld.source_title.strip().replace("\n", " ")
            if len(title) > 200:
                title = title[:200] + "…"
            lines.append(f"  source_title: {title}")
        ctx = (ld.source_text or "").strip().replace("\n", " ")
        if len(ctx) > MAX_CONTEXT_CHARS:
            ctx = ctx[:MAX_CONTEXT_CHARS] + "…"
        if ctx:
            lines.append(f"  source_context: {ctx}")
        parts.append("\n".join(lines))
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


def _build_negative_pattern(negative_locations: list[str]) -> re.Pattern | None:
    """Compile a case-insensitive substring regex from the campaign's
    excluded keywords. Returns None when there's nothing to exclude.

    Substring (not word-bounded) match by design: it mirrors the LLM's
    semantic match — if the user lists "India" they mean to also catch
    "Indian", "Indians", "Indianapolis", etc. They've already curated the
    list with the variants they care about.
    """
    if not negative_locations:
        return None
    parts = [re.escape(s.strip()) for s in negative_locations if s and s.strip()]
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)


def _classify_for_clamp(lead: Lead, neg_pattern: re.Pattern | None) -> str | None:
    """Decide whether a lead can be hard-clamped to all-zero scores without an
    LLM call. Returns the reason string when clamping applies, else None.

    Clamping rules — both are deterministic ground-truth, no judgment needed:
      1) "dead_invite": WA enrichment ran (last_validated_at IS NOT NULL) and
         confirmed the invite is dead (verified_group_name IS NULL). No point
         scoring an invite that will never resolve to a joinable group.
      2) "negative_location": any lead text/url/title/verified field contains
         an excluded keyword. The system prompt already told the LLM to clamp
         geo_fit ≤ 10 in this case; doing it deterministically saves the
         entire LLM call (relevance + engagement + structured-output round-trip).
    """
    if lead.last_validated_at is not None and lead.verified_group_name is None:
        return "dead_invite"
    if neg_pattern is not None:
        haystack = " ".join(
            filter(
                None,
                [
                    lead.verified_group_name,
                    lead.verified_group_description,
                    lead.source_title,
                    lead.source_url,
                    lead.source_text,
                ],
            )
        )
        if haystack and neg_pattern.search(haystack):
            return "negative_location"
    return None


async def _hard_clamp_zero(leads_with_reason: list[tuple[Lead, str]]) -> int:
    """Set relevance/geo_fit/engagement/total_score to 0 and stamp
    last_scored_at. Used for leads that can be ruled out without LLM judgment
    (confirmed-dead invites and negative-location matches). Returns the count
    of leads clamped."""
    if not leads_with_reason:
        return 0
    now = datetime.now(timezone.utc)
    async with SessionLocal() as s:
        for lead, _reason in leads_with_reason:
            await s.execute(
                update(Lead)
                .where(Lead.id == lead.id)
                .values(
                    relevance=0,
                    geo_fit=0,
                    engagement=0,
                    total_score=0,
                    last_scored_at=now,
                )
            )
        await s.commit()

    breakdown: dict[str, int] = {}
    for _, r in leads_with_reason:
        breakdown[r] = breakdown.get(r, 0) + 1
    logger.info("stage 6: hard-clamped %d leads (%s)", len(leads_with_reason), breakdown)
    return len(leads_with_reason)


def _eligible_leads_filter(model=Lead):
    """SQL predicate: a lead is eligible for (re-)scoring if either:
      a) it has never been scored (last_scored_at IS NULL), OR
      b) it has been re-validated AFTER its last scoring — meaning the WA
         enrichment pass produced a verified group_name/description that
         the prior score didn't see. Re-scoring with verified data is much
         more precise than the original pre-enrichment snippet-only score.

    Dead invites (last_validated_at set, verified_group_name IS NULL) are
    NOT re-scored — there's no new information that would change relevance.
    """
    return or_(
        model.last_scored_at.is_(None),
        (model.last_validated_at.isnot(None))
        & (model.verified_group_name.isnot(None))
        & (
            (model.last_scored_at.is_(None))
            | (model.last_validated_at > model.last_scored_at)
        ),
    )


async def score_for_campaign(campaign_id: int) -> dict[str, int]:
    """Stage 6: score every (re-)eligible lead for a campaign.

    Eligibility (see `_eligible_leads_filter`):
      - never-scored leads (last_scored_at IS NULL), and
      - leads enriched AFTER they were last scored — verified group_name/
        description is much higher-signal than scraped snippets, so we
        re-score with the better data.

    Pre-LLM hard-clamp: leads that are confirmed dead (enriched with no
    verified group_name) or that contain an excluded-location keyword get
    zero scores written directly without burning an LLM call. See
    `_classify_for_clamp`. Saves 20-40% of scoring tokens on geo-restricted
    campaigns and ~5-15% on enriched campaigns with stale dead invites.

    Robust: gpt-4o-mini occasionally returns empty `scores` arrays under
    structured-output mode. We retry up to MAX_ATTEMPTS over the still-eligible
    rows; each retry re-queries the filter so successfully-scored leads from
    a partial prior pass aren't redone.

    Returns counts:
      - scored: total leads now have last_scored_at set this run (LLM + clamped)
      - clamped: subset of `scored` that bypassed the LLM
      - missing: leads still pending after MAX_ATTEMPTS (LLM kept failing)
      - skipped: reserved for future use
    """
    async with SessionLocal() as s:
        campaign = await s.get(Campaign, campaign_id)
        if campaign is None:
            raise ValueError(f"campaign {campaign_id} not found")

    neg_pattern = _build_negative_pattern(campaign.negative_locations)

    # Pre-LLM clamp pass: pull eligible leads, partition into clamp vs. LLM.
    async with SessionLocal() as s:
        eligible = list(
            (
                await s.execute(
                    select(Lead).where(
                        Lead.campaign_id == campaign_id,
                        _eligible_leads_filter(),
                    )
                )
            ).scalars()
        )

    to_clamp: list[tuple[Lead, str]] = []
    needs_llm: list[Lead] = []
    for ld in eligible:
        reason = _classify_for_clamp(ld, neg_pattern)
        if reason is None:
            needs_llm.append(ld)
        else:
            to_clamp.append((ld, reason))

    clamped_count = await _hard_clamp_zero(to_clamp)
    total_scored = clamped_count
    last_remaining = -1

    for attempt in range(MAX_ATTEMPTS):
        if not needs_llm:
            break

        if len(needs_llm) == last_remaining:
            # Made no progress on the last attempt — bail before burning more tokens.
            logger.warning(
                "stage 6: %d leads still unscored after %d attempts for campaign %s",
                len(needs_llm), attempt, campaign_id,
            )
            break
        last_remaining = len(needs_llm)

        scored_this_pass = await _score_one_pass(campaign, needs_llm)
        total_scored += scored_this_pass
        logger.info(
            "stage 6 attempt %d for campaign %s: scored %d of %d (llm)",
            attempt + 1, campaign_id, scored_this_pass, len(needs_llm),
        )

        # Re-fetch the still-unscored subset for the next attempt — successful
        # rows from this pass already have last_scored_at set.
        async with SessionLocal() as s:
            needs_llm = list(
                (
                    await s.execute(
                        select(Lead).where(
                            Lead.campaign_id == campaign_id,
                            _eligible_leads_filter(),
                        )
                    )
                ).scalars()
            )
            # Re-apply the clamp filter in case enrichment landed concurrently.
            needs_llm = [
                ld for ld in needs_llm if _classify_for_clamp(ld, neg_pattern) is None
            ]

    async with SessionLocal() as s:
        still_unscored = await s.scalar(
            select(func.count(Lead.id)).where(
                Lead.campaign_id == campaign_id,
                _eligible_leads_filter(),
            )
        )
    counts = {
        "scored": total_scored,
        "clamped": clamped_count,
        "missing": still_unscored or 0,
        "skipped": 0,
    }
    logger.info("stage 6 done for campaign %s: %s", campaign_id, counts)
    return counts
