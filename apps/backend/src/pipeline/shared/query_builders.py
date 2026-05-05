"""Generic query-construction helpers used by every platform's stage 1.

Each platform's `queries.py` is a thin wrapper that picks an `anchor` (the
substring Google should lean on — `chat.whatsapp.com` for WhatsApp,
`discord.gg` for Discord, etc.) and calls the helpers here.

Why this lives in `shared/`: the 1A LLM term-expansion prompt, the 1B
Python combinator, the dedup logic, the platform-yield weighting, the
budget sizing — none of these change across platforms. Only the anchor and
the platform's own entry-point name differ. Keeping the algo in one place
means a bug fix touches one file, not three.

The anchor is passed UNQUOTED on purpose: Serper rejects literal-phrase
forms like `"chat.whatsapp.com"` with a 400 error on the starter tier.
Unquoted, the substring still pulls Google toward invite-bearing pages
and snippets often surface the full URL anyway.
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from src.clients.openai_client import QUERY_GEN_MODEL, get_openai_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — same for every platform
# ---------------------------------------------------------------------------


# Platforms that contribute `site:` constraints in T1. Per real-campaign
# data (campaign 2 vs campaign 3), social platforms (facebook/linkedin/
# twitter/x) return ~0 invites for the query budget they consume — invites
# mostly aren't there, and snippets get truncated before any link is
# visible. Reddit, Meetup, and Eventbrite are where invites actually live.
ALLOWED_QUERY_PLATFORMS: tuple[str, ...] = ("reddit", "meetup", "eventbrite")

# Per-platform yield weighting inside T1. Reddit gets the most slots (highest
# organic invite density + free fetch via asyncpraw), then meetup (top Google-
# snippet performer per the data), then eventbrite.
PLATFORM_WEIGHTS: dict[str, float] = {
    "reddit": 0.45,
    "meetup": 0.35,
    "eventbrite": 0.20,
}

# Tier weights — fraction of total query budget allocated to each template.
# T1 + T2 are the two snippet-hit paths: both carry the platform anchor so
# Google leans toward invite-bearing pages and the URL often appears in the
# snippet verbatim.
TIER_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("T1", 0.65),  # precision: anchor + quoted term + site:
    ("T2", 0.35),  # broad-net snippet-hit: anchor + quoted term, no site:
)

# Cap on industry-term variants returned by 1A. Beyond ~10 the variants start
# overlapping each other; below 6 the combinator runs out of cartesian space.
MAX_TERMS_PER_INDUSTRY = 10
MIN_TERMS_PER_INDUSTRY = 6

# Cap on group-name length we'll accept as a snowball seed. Names longer than
# this won't fit cleanly in a quoted Google query alongside the anchor and
# operators, and group names that are essentially sentences tend to be SEO-
# junk anyway.
MAX_GROUP_NAME_AS_TERM_LEN = 60

# Default cap on snowball-spawned queries per call. Snowball augments,
# doesn't replace — keeps budget impact bounded.
SNOWBALL_DEFAULT_TARGET = 60


# ---------------------------------------------------------------------------
# 1A — LLM term expansion (platform-agnostic — pure vocabulary work)
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


async def expand_industry_terms(industry: str) -> list[str]:
    """Stage 1A — one LLM call returning 6-10 quoted-able phrasings of the
    given industry seed. On any LLM failure we fall back to just the seed
    (still produces a working campaign, just with less variety).

    Same prompt for every platform — we're expanding industry vocabulary,
    which doesn't depend on which invite ecosystem we're hunting in.
    """
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
        if industry.lower() not in seen:
            out.insert(0, industry)
    return out[:MAX_TERMS_PER_INDUSTRY]


# ---------------------------------------------------------------------------
# 1B — Python combinator (parameterized on the anchor)
# ---------------------------------------------------------------------------


def _resolve_query_platforms(campaign_platforms: list[str] | None) -> list[str]:
    """Return the subset of campaign.platforms that contributes `site:` queries.

    Anything not in ALLOWED_QUERY_PLATFORMS is silently ignored. If the
    intersection is empty (e.g. legacy campaign), default to all three so
    the campaign still runs.
    """
    if not campaign_platforms:
        return list(ALLOWED_QUERY_PLATFORMS)
    chosen = {p.lower().strip() for p in campaign_platforms}
    selected = [p for p in ALLOWED_QUERY_PLATFORMS if p in chosen]
    return selected or list(ALLOWED_QUERY_PLATFORMS)


_NEG_TERM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


def _format_negatives(neg_locations: list[str] | None) -> str:
    """Render negatives as `-foo -bar` Google operators. Drops anything with
    spaces or weird chars (Google's `-` only takes single tokens). Lowercased."""
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


def _normalize_for_dedup(query: str) -> str:
    """Dedup key: lowercase + sorted whitespace tokens. Catches near-duplicates
    where word order is the only difference."""
    return " ".join(sorted(query.lower().split()))


def _platforms_cycled(platforms: list[str], rounds: int) -> list[str]:
    """Yield platforms in weight-proportional order. Reddit appears more often
    than meetup, which appears more often than eventbrite — matches yield."""
    if not platforms:
        return []
    SCALE = 20
    pool: list[str] = []
    for p in platforms:
        slots = max(1, round(PLATFORM_WEIGHTS.get(p, 0.33) * SCALE))
        pool.extend([p] * slots)
    if not pool:
        return []
    repeats = (rounds + len(pool) - 1) // len(pool)
    return (pool * max(1, repeats))[:rounds]


def build_queries(
    terms: list[str],
    platforms: list[str],
    negatives: list[str] | None,
    target_count: int,
    *,
    anchor: str,
) -> list[tuple[str, str]]:
    """Stage 1B — produce up to `target_count` queries combining `terms`
    × `platforms` × the 2 templates, weighted by tier and platform yield.

    `anchor` is the platform-specific substring (e.g. "chat.whatsapp.com",
    "discord.gg") injected unquoted into both T1 and T2 templates. See module
    docstring for why unquoted.

    Returns (query_text, source_platform) pairs where source_platform is
    "reddit" if the query targets reddit.com, else "web" (matches the legacy
    column semantics).
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

    def _t1(term: str, platform: str) -> str:
        base = f'{anchor} "{term}" site:{platform}.com'
        return f"{base} {neg}".strip() if neg else base

    def _t2(term: str) -> str:
        base = f'{anchor} "{term}"'
        return f"{base} {neg}".strip() if neg else base

    targets = {tier: max(1, round(target_count * w)) for tier, w in TIER_WEIGHTS}

    # T1 — precision: terms × platforms.
    t1_capacity = len(terms) * len(platforms)
    t1_target = min(targets["T1"], t1_capacity)
    t1_done = 0
    rotation = _platforms_cycled(platforms, t1_target)
    for i, platform in enumerate(rotation):
        if t1_done >= t1_target:
            break
        term = terms[i % len(terms)]
        if _push(_t1(term, platform), platform if platform == "reddit" else "web"):
            t1_done += 1

    # T2 — broad-net: one query per term, no site: constraint.
    t2_target = min(targets["T2"], len(terms))
    t2_done = 0
    for term in terms:
        if t2_done >= t2_target:
            break
        if _push(_t2(term), "web"):
            t2_done += 1

    return out[:target_count]


def query_target_count(max_credits_serper: int) -> int:
    """Pick a target query count that fills ~60% of the Serper budget.
    Clamped to [25, 200]."""
    return max(25, min(200, int(0.6 * max_credits_serper)))
