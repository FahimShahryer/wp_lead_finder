"""Stage 1 — query generation.

Architecture: **1A (LLM term expansion) + 1B (Python combinator)**.

The LLM is good at vocabulary expansion ("8 ways to phrase a SaaS founder?")
but bad at composing 30 narrow Google queries with consistent operator
placement — it tries to be "creative" and ends up broad. So we split:

  1A — one cheap LLM call per industry, returning 6-10 specific quoted-able
       phrasings (e.g. "marketing agency" → ["marketing agency", "agency
       owners", "agency founders", ...]). Pure vocabulary work.

  1B — Python cartesian product over (terms × platforms × templates),
       producing queries with reliably-consistent operators. No LLM.

Why this is better than a single big LLM call:
  - Every query has the highest-precision operators (`"chat.whatsapp.com"`
    anchor + quoted industry term + `site:` constraint + negatives).
  - No more "the LLM forgot to quote the term" or "the LLM dropped the anchor
    on this one query" failure modes.
  - Cost is the same (~1 small LLM call per campaign vs the old single call).
  - Empirical data: campaigns with narrow operator-rich queries return 10×
    more leads than broad-keyword queries (see SEARCH_STRATEGY_NOTES.md).
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.clients.openai_client import QUERY_GEN_MODEL, get_openai_client
from src.db.models import Campaign, Query

logger = logging.getLogger(__name__)


# Platforms that contribute `site:` constraints in T1. Per the data
# (campaign 2 vs campaign 3), social platforms (facebook/linkedin/twitter/x)
# return ~0 invites for the query budget they consume — invites mostly aren't
# there, and snippets get truncated before the link is visible. Reddit, Meetup,
# and Eventbrite are where invites actually live.
ALLOWED_QUERY_PLATFORMS: tuple[str, ...] = ("reddit", "meetup", "eventbrite")

# Per-platform yield weighting inside T1. Reddit gets the most slots (highest
# organic invite density + free fetch via asyncpraw), then meetup (top Google-
# snippet performer per the data), then eventbrite (similar pattern, lower
# density).
PLATFORM_WEIGHTS: dict[str, float] = {
    "reddit": 0.45,
    "meetup": 0.35,
    "eventbrite": 0.20,
}

# Tier weights — fraction of total query budget allocated to each template.
# T1 + T2 are the two snippet-hit paths: both carry the literal-phrase
# `"chat.whatsapp.com"` anchor so Google returns pages with the invite link
# directly visible in the snippet. We dropped a third tier (loose
# whatsapp-keyword queries with no anchor) — it spent budget for ~0 yield
# because pages without the anchor in the snippet require fetching, and the
# keyword-stuffed queries narrow Google's index in the wrong dimension.
TIER_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("T1", 0.65),  # precision: anchor + quoted term + site:
    ("T2", 0.35),  # broad-net snippet-hit: anchor + quoted term, no site:
)

# Cap on industry-term variants returned by 1A. Beyond ~10 the variants start
# overlapping each other; below 6 the combinator runs out of cartesian space.
MAX_TERMS_PER_INDUSTRY = 10
MIN_TERMS_PER_INDUSTRY = 6


# ---------------------------------------------------------------------------
# 1A — LLM term expansion
# ---------------------------------------------------------------------------


class IndustryTermExpansion(BaseModel):
    terms: list[str] = Field(
        description=(
            "6-10 distinct, specific, quoted-able phrasings of the same audience "
            "as the seed industry. Each item is a single short phrase."
        )
    )


EXPANSION_SYSTEM_PROMPT = """\
You expand a single industry seed into 6-10 specific quoted-able variants
that target the SAME audience but vary in phrasing.

Examples:

  Seed: "marketing agency"
  Out:  ["marketing agency", "marketing agencies", "agency owners",
         "agency founders", "ad agency owners", "digital agency",
         "creative agency", "marketing consultancy"]

  Seed: "SaaS"
  Out:  ["SaaS founders", "B2B SaaS", "indie SaaS", "micro SaaS",
         "SaaS startup", "vertical SaaS", "SaaS bootstrappers",
         "software entrepreneurs"]

Rules:
- Each variant MUST be a single short phrase (under 40 characters) suitable
  for use inside Google's literal-phrase double quotes.
- Variants must stay TIGHT to the seed audience. Don't drift — "marketing
  agency" should not produce "ecommerce" or "growth hacking" unless those
  are essentially synonyms.
- No commentary, no descriptions, no full sentences.
- No over-broad terms like just "tech", "business", "startups".
- Prefer real-world phrasings a practitioner would self-identify with.
"""


async def _expand_industry_terms(industry: str) -> list[str]:
    """Stage 1A — one LLM call returning 6-10 quoted-able phrasings of the
    given industry seed. On any LLM failure we fall back to just the seed
    (still produces a working campaign, just with less variety)."""
    client = get_openai_client()
    try:
        completion = await client.chat.completions.parse(
            model=QUERY_GEN_MODEL,
            messages=[
                {"role": "system", "content": EXPANSION_SYSTEM_PROMPT},
                {"role": "user", "content": f"Seed industry: {industry}"},
            ],
            response_format=IndustryTermExpansion,
        )
    except Exception as e:
        logger.warning("1A expansion failed for %r, falling back to seed: %s", industry, e)
        return [industry]

    parsed = completion.choices[0].message.parsed
    if parsed is None:
        return [industry]

    seen: set[str] = set()
    out: list[str] = []
    for raw in parsed.terms:
        term = raw.strip().strip('"').strip()
        if not term or len(term) > 40:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)

    if len(out) < MIN_TERMS_PER_INDUSTRY:
        # Model returned too few; ensure the seed itself is present.
        if industry.lower() not in seen:
            out.insert(0, industry)
    return out[:MAX_TERMS_PER_INDUSTRY]


# ---------------------------------------------------------------------------
# 1B — Python combinator
# ---------------------------------------------------------------------------


def _resolve_query_platforms(campaign_platforms: list[str] | None) -> list[str]:
    """Return the subset of campaign.platforms that contributes `site:` queries.

    Anything not in ALLOWED_QUERY_PLATFORMS is silently ignored. This includes:
      - "web" (implicit; means "no site: constraint" — covered by T2)
      - "facebook" / "linkedin" / "twitter" / "x" (legacy values from older
        campaigns; new campaigns can't pick these. They're known bad-ROI per
        the campaign 2 data — invites don't live there.)

    If the intersection is empty (e.g. legacy campaign with only "facebook"
    selected), default to all three allowed platforms so the campaign still
    runs.
    """
    if not campaign_platforms:
        return list(ALLOWED_QUERY_PLATFORMS)
    chosen = {p.lower().strip() for p in campaign_platforms}
    selected = [p for p in ALLOWED_QUERY_PLATFORMS if p in chosen]
    return selected or list(ALLOWED_QUERY_PLATFORMS)


_NEG_TERM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


def _format_negatives(neg_locations: list[str] | None) -> str:
    """Render negatives as `-foo -bar` Google operators. Drops anything that
    contains spaces / weird chars (Google's `-` operator only takes single
    tokens). Lowercased for cleanliness — Google's not case-sensitive."""
    if not neg_locations:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for raw in neg_locations:
        token = raw.strip().lower()
        if not token or not _NEG_TERM_RE.fullmatch(token) or token in seen:
            continue
        seen.add(token)
        parts.append(f"-{token}")
    return " ".join(parts)


def _build_query_t1(term: str, platform: str, neg: str) -> str:
    """T1: precision — anchor + quoted term + site:platform + negatives."""
    base = f'"chat.whatsapp.com" "{term}" site:{platform}.com'
    return f"{base} {neg}".strip() if neg else base


def _build_query_t2(term: str, neg: str) -> str:
    """T2: broad-net snippet-hit — anchor + quoted term + negatives, no site:."""
    base = f'"chat.whatsapp.com" "{term}"'
    return f"{base} {neg}".strip() if neg else base


def _normalize_for_dedup(query: str) -> str:
    """Dedup key: lowercase + sorted whitespace tokens. Catches near-duplicates
    where word order is the only difference."""
    return " ".join(sorted(query.lower().split()))


def _platforms_cycled(platforms: list[str], rounds: int) -> list[str]:
    """Yield platforms in weight-proportional order. Reddit appears more often
    than meetup, which appears more often than eventbrite — matches the yield
    weights in PLATFORM_WEIGHTS."""
    if not platforms:
        return []
    # Build a flat ordered list weighted by PLATFORM_WEIGHTS. We use a multiplier
    # of 20 (so 0.45 → 9 slots, 0.35 → 7, 0.20 → 4) to give a clean rotation.
    SCALE = 20
    pool: list[str] = []
    for p in platforms:
        slots = max(1, round(PLATFORM_WEIGHTS.get(p, 0.33) * SCALE))
        pool.extend([p] * slots)
    # Repeat the pool enough times to satisfy `rounds`.
    if not pool:
        return []
    repeats = (rounds + len(pool) - 1) // len(pool)
    return (pool * max(1, repeats))[:rounds]


def build_queries(
    terms: list[str],
    platforms: list[str],
    negatives: list[str] | None,
    target_count: int,
) -> list[tuple[str, str]]:
    """Stage 1B — produce up to `target_count` queries combining `terms`
    × `platforms` × the 3 templates, weighted by tier and platform yield.

    Returns a list of (query_text, source_platform) pairs where source_platform
    is "reddit" if the query targets reddit.com (matches the legacy column
    semantics) or "web" otherwise.
    """
    if not terms or target_count <= 0:
        return []

    platforms = [p for p in platforms if p in PLATFORM_WEIGHTS] or list(ALLOWED_QUERY_PLATFORMS)
    neg = _format_negatives(negatives)

    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _push(query: str, source_platform: str) -> bool:
        if len(query) > 200:
            return False
        key = _normalize_for_dedup(query)
        if key in seen:
            return False
        seen.add(key)
        out.append((query, source_platform))
        return True

    # Compute target counts per tier.
    targets = {tier: max(1, round(target_count * w)) for tier, w in TIER_WEIGHTS}

    # T1 — precision queries: terms × platforms (cartesian).
    # Capped both by the tier target (e.g. 65% of budget) and by the actual
    # cartesian space (terms × platforms). For narrow industries this caps
    # naturally without producing duplicates.
    t1_capacity = len(terms) * len(platforms)
    t1_target = min(targets["T1"], t1_capacity)
    t1_done = 0
    # Iterate platforms-major so we get diversity early (one query per platform
    # before repeating the same platform with a new term).
    rotation = _platforms_cycled(platforms, t1_target)
    for i, platform in enumerate(rotation):
        if t1_done >= t1_target:
            break
        term = terms[i % len(terms)]
        if _push(_build_query_t1(term, platform, neg), platform if platform == "reddit" else "web"):
            t1_done += 1

    # T2 — broad-net: one query per term, no site: constraint.
    t2_target = min(targets["T2"], len(terms))
    t2_done = 0
    for term in terms:
        if t2_done >= t2_target:
            break
        if _push(_build_query_t2(term, neg), "web"):
            t2_done += 1

    return out[:target_count]


# ---------------------------------------------------------------------------
# Budget sizing
# ---------------------------------------------------------------------------


def _query_target_count(max_credits_serper: int) -> int:
    """Pick a target query count that fills ~60% of the Serper budget,
    leaving headroom for retries. Clamped to [25, 200] — below 25 the
    cartesian space is too small to dedupe meaningfully, above 200 we hit
    diminishing returns + LLM output-token concerns are moot since we're
    not using the LLM for query construction anymore."""
    return max(25, min(200, int(0.6 * max_credits_serper)))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def generate_queries(session: AsyncSession, campaign: Campaign) -> list[Query]:
    """Stage 1: ICP -> N search queries persisted in `queries` (status='pending').

    Two-phase pipeline:
      1A — LLM expands each industry into 6-10 quoted-able variants.
      1B — Python combinator builds queries from (terms × platforms × templates).

    Idempotent: if the campaign already has queries, returns them without
    re-calling the LLM. Re-running the stage costs zero LLM tokens.
    """
    existing_count = await session.scalar(
        select(func.count(Query.id)).where(Query.campaign_id == campaign.id)
    )
    if existing_count and existing_count > 0:
        logger.info(
            "campaign %s already has %d queries, skipping 1A+1B",
            campaign.id, existing_count,
        )
        result = await session.execute(select(Query).where(Query.campaign_id == campaign.id))
        return list(result.scalars().all())

    # 1A — expand each industry seed. One LLM call per industry; for the
    # user's typical single-industry campaign this is exactly one call.
    industries = [s.strip() for s in (campaign.industries or []) if s and s.strip()]
    if not industries:
        logger.warning("campaign %s has no industries — generating no queries", campaign.id)
        return []

    seen_terms: set[str] = set()
    all_terms: list[str] = []
    for industry in industries:
        for term in await _expand_industry_terms(industry):
            key = term.lower()
            if key in seen_terms:
                continue
            seen_terms.add(key)
            all_terms.append(term)

    # 1B — combinator.
    platforms = _resolve_query_platforms(campaign.platforms)
    target = _query_target_count(campaign.max_credits_serper)
    pairs = build_queries(all_terms, platforms, campaign.negative_locations, target)

    rows = [
        Query(
            campaign_id=campaign.id,
            query_text=q,
            source_platform=src,
            status="pending",
        )
        for q, src in pairs
    ]
    session.add_all(rows)
    await session.commit()
    logger.info(
        "stage 1 done for campaign %s: %d terms × %d platforms → %d queries",
        campaign.id, len(all_terms), len(platforms), len(rows),
    )
    return rows
