import asyncio
import logging
import re

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.clients.openai_client import QUERY_GEN_MODEL, get_openai_client
from src.db.models import Campaign, Lead, LeadTag, Tag
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

BATCH_SIZE = 12
LLM_CONCURRENCY = 5
MAX_CONTEXT_CHARS = 500
MAX_TAGS_PER_LEAD = 3
TAG_NAME_RE = re.compile(r"[^a-z0-9 +&-]")

# Reserved tag names. Auto-managed (mutually exclusive) on every auto-tag run:
#   - industry: lead carries any tag that matches a campaign industry
#   - business: lead has no industry tag but at least one BUSINESS_TAG_NAMES match
#   - misc:     lead has neither
# Gives the user three single-click buckets to triage/export against.
MISC_TAG_NAME = "misc"
BUSINESS_TAG_NAME = "business"
# Taxonomy used to classify a lead as "business-adjacent" when it doesn't
# carry the campaign's industry tag. Kept lowercased; matched verbatim against
# a lead's other auto-tag names. Conservative on purpose — too broad and the
# misc bucket disappears.
BUSINESS_TAG_NAMES: frozenset[str] = frozenset({
    "business",
    "networking",
    "startups",
    "startup",
    "entrepreneurship",
    "entrepreneurs",
    "founders",
    "investors",
    "investing",
    "finance",
    "venture capital",
    "vc",
    "jobs",
    "careers",
    "career development",
    "saas",
    "microsaas",
    "tech",
    "marketing",
    "sales",
    "ecommerce",
    "e-commerce",
    "b2b",
    "b2c",
    "freelance",
    "freelancers",
    "consulting",
    "coworking",
})


class TagAssignment(BaseModel):
    invite_id: str = Field(description="The lead's invite_id, copied verbatim")
    tags: list[str] = Field(
        description=(
            "1-3 short categorical labels (1-3 words each), e.g. 'ai', 'business', "
            "'networking', 'jobs'. Lowercased. Empty if no clear category."
        )
    )


class TagBatch(BaseModel):
    assignments: list[TagAssignment]


SYSTEM_PROMPT = """\
You categorize WhatsApp groups into short tag labels.

Rules:
- 1 to 3 tags per group, each 1-3 words, lowercased.
- Prefer tags from the SUGGESTED set when they fit.
- It's OK to add a new tag if none of the suggested ones fit, but be conservative.
- Skip vague tags ('community', 'group') unless that's the only meaningful signal.
- Return one entry per input invite_id, copied verbatim.
"""


def _normalize_tag(name: str) -> str:
    name = name.strip().lower()
    name = TAG_NAME_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:40]


def _format_lead_block(leads: list[Lead]) -> str:
    """Each lead presented to the LLM as: verified group name (when present)
    + verified description (rare) + scraped source-text context. Verified
    name is the highest-signal channel — it's what the group ACTUALLY calls
    itself, not just what the scraper saw nearby."""
    parts: list[str] = []
    for ld in leads:
        lines = [f"[{ld.invite_id}]"]
        if ld.verified_group_name:
            lines.append(f"  group_name: {ld.verified_group_name}")
        if ld.verified_group_description:
            lines.append(f"  group_description: {ld.verified_group_description}")
        ctx = (ld.source_text or "").strip().replace("\n", " ")
        if len(ctx) > MAX_CONTEXT_CHARS:
            ctx = ctx[:MAX_CONTEXT_CHARS] + "…"
        if ctx:
            lines.append(f"  source_context: {ctx}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _build_user_msg(campaign: Campaign, leads: list[Lead], existing_tags: list[str]) -> str:
    suggested = sorted(set(_normalize_tag(t) for t in (campaign.industries or []) + existing_tags))
    suggested = [t for t in suggested if t]
    return (
        f"Suggested tags: {', '.join(suggested) if suggested else '(none yet — propose your own)'}\n"
        f"Campaign industries: {', '.join(campaign.industries) or '(none)'}\n\n"
        f"Tag these groups:\n\n{_format_lead_block(leads)}"
    )


async def _tag_batch(
    campaign: Campaign, batch: list[Lead], existing_tags: list[str]
) -> list[TagAssignment]:
    client = get_openai_client()
    completion = await client.chat.completions.parse(
        model=QUERY_GEN_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_msg(campaign, batch, existing_tags)},
        ],
        response_format=TagBatch,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        return []
    return parsed.assignments


async def _ensure_managed_tag(
    s: AsyncSession, campaign_id: int, name: str
) -> Tag:
    tag = await s.scalar(
        select(Tag).where(Tag.campaign_id == campaign_id, Tag.name == name)
    )
    if tag is None:
        tag = Tag(campaign_id=campaign_id, name=name)
        s.add(tag)
        await s.flush()
    return tag


async def _set_membership(
    s: AsyncSession, tag_id: int, target_lead_ids: set[int], universe: set[int]
) -> tuple[int, int]:
    """Make `target_lead_ids` exactly the set of leads (within `universe`)
    that have `tag_id`. Inserts missing, deletes extras. Returns (attached, detached)."""
    current = set(
        (
            await s.execute(
                select(LeadTag.lead_id).where(
                    LeadTag.tag_id == tag_id,
                    LeadTag.lead_id.in_(universe) if universe else LeadTag.lead_id.is_(None),
                )
            )
        ).scalars()
    ) if universe else set()

    to_attach = target_lead_ids - current
    to_detach = current - target_lead_ids

    attached = 0
    for lid in to_attach:
        result = await s.execute(
            pg_insert(LeadTag)
            .values(lead_id=lid, tag_id=tag_id)
            .on_conflict_do_nothing(index_elements=["lead_id", "tag_id"])
        )
        if result.rowcount:
            attached += result.rowcount

    detached = 0
    if to_detach:
        det = await s.execute(
            delete(LeadTag).where(
                LeadTag.tag_id == tag_id,
                LeadTag.lead_id.in_(to_detach),
            )
        )
        detached = det.rowcount or 0
    return attached, detached


async def _refresh_managed_tags(
    s: AsyncSession, campaign: Campaign
) -> dict[str, tuple[int, int]]:
    """Reconcile the auto-managed pseudo-tags (industry, business, misc) for
    the campaign's live scored leads. Mutually exclusive: a lead is in exactly
    one of {industry-tagged, business, misc}. Industry membership is derived
    from real tags whose names match campaign.industries — those tags are
    untouched here; we only manage the 'business' and 'misc' bucket tags."""
    industries = sorted({_normalize_tag(i) for i in (campaign.industries or [])})
    industries = [i for i in industries if i]
    if not industries:
        return {}

    eligible = set(
        (
            await s.execute(
                select(Lead.id).where(
                    Lead.campaign_id == campaign.id,
                    Lead.last_scored_at.isnot(None),
                    ~(Lead.last_validated_at.isnot(None) & Lead.verified_group_name.is_(None)),
                )
            )
        ).scalars()
    )
    if not eligible:
        return {}

    industry_tag_ids = list(
        (
            await s.execute(
                select(Tag.id).where(
                    Tag.campaign_id == campaign.id, Tag.name.in_(industries)
                )
            )
        ).scalars()
    )
    has_industry: set[int] = set()
    if industry_tag_ids:
        has_industry = set(
            (
                await s.execute(
                    select(LeadTag.lead_id).where(
                        LeadTag.lead_id.in_(eligible),
                        LeadTag.tag_id.in_(industry_tag_ids),
                    )
                )
            ).scalars()
        ) & eligible

    # Find auto-tag tags whose names are in the business taxonomy. These are
    # *real* tags created by the LLM pass; we read them as a signal, not own them.
    business_signal_tag_ids = list(
        (
            await s.execute(
                select(Tag.id).where(
                    Tag.campaign_id == campaign.id,
                    Tag.name.in_(BUSINESS_TAG_NAMES),
                )
            )
        ).scalars()
    )
    has_business_signal: set[int] = set()
    if business_signal_tag_ids:
        has_business_signal = set(
            (
                await s.execute(
                    select(LeadTag.lead_id).where(
                        LeadTag.lead_id.in_(eligible),
                        LeadTag.tag_id.in_(business_signal_tag_ids),
                    )
                )
            ).scalars()
        ) & eligible

    business_set = (has_business_signal - has_industry)
    misc_set = eligible - has_industry - business_set

    out: dict[str, tuple[int, int]] = {}

    if business_set or business_signal_tag_ids:
        business_tag = await _ensure_managed_tag(s, campaign.id, BUSINESS_TAG_NAME)
        out["business"] = await _set_membership(s, business_tag.id, business_set, eligible)

    if misc_set:
        misc_tag = await _ensure_managed_tag(s, campaign.id, MISC_TAG_NAME)
        out["misc"] = await _set_membership(s, misc_tag.id, misc_set, eligible)
    else:
        # No misc leads — still detach any stale misc rows if the tag exists.
        misc_tag = await s.scalar(
            select(Tag).where(
                Tag.campaign_id == campaign.id, Tag.name == MISC_TAG_NAME
            )
        )
        if misc_tag is not None:
            out["misc"] = await _set_membership(s, misc_tag.id, set(), eligible)

    return out


async def auto_tag_campaign(campaign_id: int, only_untagged: bool = True) -> dict[str, int]:
    """Run an LLM pass over the campaign's leads and assign 1-3 short tags per lead.
    Tags are persisted via tags + lead_tags. By default operates on leads that have
    no tags yet; pass only_untagged=False to retag everything."""
    async with SessionLocal() as s:
        campaign = await s.get(Campaign, campaign_id)
        if campaign is None:
            raise ValueError(f"campaign {campaign_id} not found")

        # Skip confirmed-dead invites: once enrichment has run AND came back
        # with no group name, the link is unjoinable and shouldn't carry tags.
        # Leads not yet validated are still tagged (they might be live).
        stmt = select(Lead).where(
            Lead.campaign_id == campaign_id,
            Lead.last_scored_at.isnot(None),
            ~(Lead.last_validated_at.isnot(None) & Lead.verified_group_name.is_(None)),
        )
        if only_untagged:
            tagged_ids = (
                select(LeadTag.lead_id)
                .join(Tag, Tag.id == LeadTag.tag_id)
                .where(Tag.campaign_id == campaign_id)
                .scalar_subquery()
            )
            stmt = stmt.where(Lead.id.notin_(tagged_ids))
        leads = list((await s.execute(stmt)).scalars())

        existing = list(
            (
                await s.execute(
                    select(Tag.name).where(Tag.campaign_id == campaign_id)
                )
            ).scalars()
        )

    if not leads:
        return {"considered": 0, "tagged": 0, "tags_created": 0, "links_created": 0}

    batches = [leads[i : i + BATCH_SIZE] for i in range(0, len(leads), BATCH_SIZE)]
    sem = asyncio.Semaphore(LLM_CONCURRENCY)

    async def _run(b: list[Lead]) -> list[TagAssignment]:
        async with sem:
            try:
                return await _tag_batch(campaign, b, existing)
            except Exception as e:
                logger.exception("auto-tag batch failed (size=%d): %s", len(b), e)
                return []

    batch_results = await asyncio.gather(*[_run(b) for b in batches])
    by_invite = {a.invite_id: a for batch in batch_results for a in batch}

    tags_created = 0
    links_created = 0
    tagged_leads = 0
    async with SessionLocal() as s:
        # Cache tag id by name for this run.
        existing_rows = list(
            (
                await s.execute(
                    select(Tag.id, Tag.name).where(Tag.campaign_id == campaign_id)
                )
            ).all()
        )
        tag_id_by_name: dict[str, int] = {r.name: r.id for r in existing_rows}

        for ld in leads:
            a = by_invite.get(ld.invite_id)
            if a is None:
                continue
            normalized = []
            for raw in a.tags[:MAX_TAGS_PER_LEAD]:
                n = _normalize_tag(raw)
                if n:
                    normalized.append(n)
            if not normalized:
                continue
            for name in normalized:
                tag_id = tag_id_by_name.get(name)
                if tag_id is None:
                    new_tag = Tag(campaign_id=campaign_id, name=name)
                    s.add(new_tag)
                    await s.flush()
                    tag_id = new_tag.id
                    tag_id_by_name[name] = tag_id
                    tags_created += 1
                # Idempotent attach — ON CONFLICT DO NOTHING.
                stmt = (
                    pg_insert(LeadTag)
                    .values(lead_id=ld.id, tag_id=tag_id)
                    .on_conflict_do_nothing(index_elements=["lead_id", "tag_id"])
                )
                result = await s.execute(stmt)
                if result.rowcount:
                    links_created += result.rowcount
            tagged_leads += 1

        # Reconcile the auto-managed pseudo-tags (business, misc) against the
        # full set of live leads — runs every auto_tag invocation, so the
        # buckets stay accurate as industries or LLM tag choices shift.
        managed = await _refresh_managed_tags(s, campaign)
        await s.commit()

    business_attached, business_detached = managed.get("business", (0, 0))
    misc_attached, misc_detached = managed.get("misc", (0, 0))

    counts = {
        "considered": len(leads),
        "tagged": tagged_leads,
        "tags_created": tags_created,
        "links_created": links_created,
        "business_attached": business_attached,
        "business_detached": business_detached,
        "misc_attached": misc_attached,
        "misc_detached": misc_detached,
    }
    logger.info("auto-tag done for campaign %s: %s", campaign_id, counts)
    return counts
