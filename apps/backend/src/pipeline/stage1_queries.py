import logging
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.clients.openai_client import QUERY_GEN_MODEL, get_openai_client
from src.db.models import Campaign, Query

logger = logging.getLogger(__name__)


class GeneratedQuery(BaseModel):
    query_text: str = Field(description="A single Google search query, ready to paste")
    source_platform: Literal["reddit", "web"] = Field(
        description="'reddit' if the query targets reddit.com via site: operator, else 'web'"
    )


class QueryGenResponse(BaseModel):
    queries: list[GeneratedQuery]


SYSTEM_PROMPT = """\
You are an expert B2B lead-prospecting analyst. Your job is to generate Google
search queries that find publicly-shared WhatsApp group invite links
(chat.whatsapp.com/<invite_id>) used by a specific industry in specific
geographies. Output queries that an experienced human prospector would write
— not generic LLM filler.
"""

USER_TEMPLATE = """\
Generate {target_low} to {target_high} distinct Google search queries to find
public WhatsApp groups matching this Ideal Customer Profile (ICP):

Target industries: {industries}
Target locations: {locations}
EXCLUDE locations / keywords: {negative_locations}
Target platforms: {platforms}

Hard requirements:
- About half of queries MUST use the `site:reddit.com` operator. Reddit is
  where the bulk of public WhatsApp invites get shared.
- The other half should target the open web (forums, blogs, community pages,
  newsletters).
- AT LEAST 30% of queries MUST include the literal-phrase token
  `"chat.whatsapp.com"` (with the double quotes — this is critical). This
  forces Google to return ONLY pages whose snippet shows the invite URL
  directly, so we can extract via regex without fetching the page. Highest-
  precision recall mechanism we have. Use these alongside both
  `site:reddit.com` AND open-web queries — they compose naturally:
    "chat.whatsapp.com" "marketing agency" site:reddit.com
    "chat.whatsapp.com" marketing agency owners group
    "chat.whatsapp.com" "marketing agency" -india
- Use the `-` operator to exclude every negative-location term above.
  Example: if "India" and "Indian" are excluded, append `-india -indian`
  to many queries.
- Vary phrasing across queries: "whatsapp group", "wa group",
  "whatsapp community", "join whatsapp", "share whatsapp invite",
  "whatsapp invite link".
- Where appropriate, mix in native-language terms for the target locations
  (Arabic for Dubai/Saudi, Spanish for Latin America, etc.).
- Reference industry phrasing naturally — "AI agency owners group",
  "marketing agency founders community", "SaaS founders chat".
- Each query MUST be under 200 characters.
- No duplicates, no near-duplicates, no commentary.{platform_extras}

Return only the structured queries.
"""

# Hard-firewall platforms: Firecrawl can't fetch them, but Google sometimes
# indexes WhatsApp invite links inside their public pages' meta-descriptions.
# We hunt for those snippets directly with literal-phrase site: queries.
HARD_FIREWALL_PLATFORMS: dict[str, str] = {
    "facebook": "site:facebook.com",
    "linkedin": "site:linkedin.com",
    "twitter": "site:twitter.com",
    "x": "site:x.com",
}

# Soft platforms — Firecrawl can fetch them, Stage 3 auto-routes to `web`.
# We just bias query gen so the funnel includes them.
SOFT_PLATFORMS: dict[str, str] = {
    "meetup": "site:meetup.com",
    "eventbrite": "site:eventbrite.com",
}


def _platform_extras_block(platforms: list[str]) -> str:
    """Build extra prompt instructions for any opted-in non-default platforms.
    Returns empty string when only reddit/web are selected (default behavior)."""
    chosen = {p.lower() for p in (platforms or [])}
    hard = [(name, op) for name, op in HARD_FIREWALL_PLATFORMS.items() if name in chosen]
    soft = [(name, op) for name, op in SOFT_PLATFORMS.items() if name in chosen]
    if not hard and not soft:
        return ""

    lines = ["", ""]

    if hard:
        lines.append("HARD-FIREWALL PLATFORMS — emit 3-5 LITERAL-PHRASE queries each:")
        for name, op in hard:
            lines.append(
                f'  - For {name}: queries like `"chat.whatsapp.com" "<industry term>" {op}`. '
                f"The literal `\"chat.whatsapp.com\"` (in quotes) is critical — it forces "
                f"Google to return ONLY pages where the invite text is in the snippet, "
                f"so we can extract without fetching the (anti-scraping) page."
            )

    if soft:
        lines.append("SOFT PLATFORMS — emit 3-5 normal queries each:")
        for name, op in soft:
            lines.append(
                f"  - For {name}: queries like `<industry> whatsapp group {op}`. "
                f"These are publicly fetchable, so plain topic queries work."
            )

    return "\n" + "\n".join(lines)


def _format_list(items: list[str], fallback: str) -> str:
    return ", ".join(items) if items else fallback


def _query_target_range(max_credits_serper: int) -> tuple[int, int]:
    """Pick a query-count target that fills ~60% of the Serper budget, leaving
    headroom for failed queries (~5% empirically) and varying yield. Clamped to
    a sane band so tiny budgets still get a useful spread and huge budgets don't
    blow the prompt's output-token budget.

      max_credits_serper=50  -> 25-35 queries
      max_credits_serper=100 -> 55-65 queries
      max_credits_serper=300 -> 175-185 queries
      max_credits_serper=500 -> 195-205 queries (capped at 200 high)

    The 200-query ceiling is sized for gpt-4o-mini's 16k output-token cap:
    each query is ~50 output tokens including JSON wrapping, so 200 queries
    fit comfortably with room for the model's internal reasoning.
    """
    target = max(30, min(200, int(0.6 * max_credits_serper)))
    return max(25, target - 5), target + 5


async def generate_queries(session: AsyncSession, campaign: Campaign) -> list[Query]:
    """Stage 1: ICP -> 25-35 search queries persisted in `queries` (status='pending').

    Idempotent: if the campaign already has queries, returns them without re-calling
    the model. Re-running the stage costs zero LLM tokens.
    """
    existing_count = await session.scalar(
        select(func.count(Query.id)).where(Query.campaign_id == campaign.id)
    )
    if existing_count and existing_count > 0:
        logger.info(
            "campaign %s already has %d queries, skipping LLM call",
            campaign.id,
            existing_count,
        )
        result = await session.execute(select(Query).where(Query.campaign_id == campaign.id))
        return list(result.scalars().all())

    target_low, target_high = _query_target_range(campaign.max_credits_serper)
    user_msg = USER_TEMPLATE.format(
        industries=_format_list(campaign.industries, "(none specified)"),
        locations=_format_list(campaign.locations, "(any)"),
        negative_locations=_format_list(campaign.negative_locations, "(none)"),
        platforms=_format_list(campaign.platforms, "reddit, web"),
        target_low=target_low,
        target_high=target_high,
        platform_extras=_platform_extras_block(campaign.platforms),
    )

    client = get_openai_client()
    completion = await client.chat.completions.parse(
        model=QUERY_GEN_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format=QueryGenResponse,
    )

    parsed = completion.choices[0].message.parsed
    if parsed is None:
        refusal = completion.choices[0].message.refusal
        raise RuntimeError(f"OpenAI did not return a parsed response (refusal={refusal!r})")

    seen: set[str] = set()
    rows: list[Query] = []
    for gq in parsed.queries:
        text = gq.query_text.strip()
        if not text or len(text) > 200:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            Query(
                campaign_id=campaign.id,
                query_text=text,
                source_platform=gq.source_platform,
                status="pending",
            )
        )

    session.add_all(rows)
    await session.commit()
    logger.info("generated %d queries for campaign %s", len(rows), campaign.id)
    return rows
