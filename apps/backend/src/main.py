import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Literal

import redis.asyncio as redis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.clients import wa_sidecar_client
from src.core.config import settings
from src.db.models import (
    BroadcastJob,
    Campaign,
    Conversation,
    ConversationTag,
    InboxTag,
    Lead,
    LeadTag,
    Message,
    OutboundMessage,
    Tag,
    WhatsAppNumber,
)
from src.db.models import Query as QueryModel
from src.db.models import SearchResult
from src.db.session import SessionLocal, engine, get_session
from src.pipeline.auto_tag import auto_tag_campaign
from src.pipeline.filter_by_prompt import filter_leads
from src.pipeline.whatsapp.enricher import (
    EnrichmentCounts,
    WhatsAppBlocked,
    WhatsAppBudgetExceeded,
    enrich_campaign,
)
from src.pipeline.whatsapp.validator import DEFAULT_REQUEST_BUDGET

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    logger.info("api: ready")
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.arq_pool.aclose()
        await engine.dispose()


app = FastAPI(title="wp2-leadfinder", lifespan=lifespan)

# Browser talks to API cross-origin during local dev. We accept both the
# default 3000 host port and the alt 3010 used when sharing the box with
# another stack that's already on 3000.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3010",
        "http://127.0.0.1:3010",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Health ----------


@app.get("/health")
async def health():
    db_status = "ok"
    redis_status = "ok"
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as e:
        logger.warning("db ping failed: %s", e)
        db_status = "fail"
    try:
        if not await app.state.redis.ping():
            redis_status = "fail"
    except Exception as e:
        logger.warning("redis ping failed: %s", e)
        redis_status = "fail"
    body = {"db": db_status, "redis": redis_status}
    code = 200 if db_status == "ok" and redis_status == "ok" else 503
    return JSONResponse(body, status_code=code)


# ---------- Schemas ----------


# Platforms that are still meaningful for query construction. Social platforms
# (facebook/linkedin/twitter/x) were removed in the stage-1 rewrite — invites
# don't live there per real-campaign data, and explicit `site:` queries against
# them wasted ~25% of the Serper budget for ~0 yield. Anything not in this set
# is silently dropped at ingestion.
ALLOWED_PLATFORMS = {"reddit", "web", "meetup", "eventbrite"}

# Per-campaign target platform — the invite-link ecosystem this campaign hunts
# in. Each platform has its own orchestrator under `pipeline/<platform>/`. New
# values land in this set when their orchestrator ships.
ALLOWED_TARGET_PLATFORMS = {"whatsapp", "discord"}


def invite_url_for(platform: str, invite_id: str) -> str:
    """Map a Lead's invite_id back to its canonical join URL. Used by every
    surface that emits a join link (CSV export today; analytics endpoints
    later). Centralized so adding a new platform is one line here."""
    if platform == "discord":
        return f"https://discord.gg/{invite_id}"
    return f"https://chat.whatsapp.com/{invite_id}"


class CreateCampaignRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    industries: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    negative_locations: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default=["reddit", "web", "meetup", "eventbrite"])
    # Which invite ecosystem to hunt in. Default 'whatsapp' for back-compat.
    # The Literal widens here as new orchestrators ship — see the dispatcher
    # in pipeline/run_campaign.py for the matching backend code.
    platform: Literal["whatsapp", "discord"] = "whatsapp"
    max_credits_serper: int = Field(default=500, ge=1)
    max_credits_firecrawl: int = Field(default=200, ge=1)

    @field_validator("platforms")
    @classmethod
    def _drop_disallowed_platforms(cls, v: list[str]) -> list[str]:
        return [p for p in (v or []) if p.lower().strip() in ALLOWED_PLATFORMS]


class CreateCampaignResponse(BaseModel):
    id: int
    status: str


class CampaignSummary(BaseModel):
    id: int
    name: str
    status: str
    current_stage: str | None
    platform: str
    leads_count: int
    scored_leads_count: int
    serper_credits_used: int
    firecrawl_credits_used: int
    created_at: datetime
    completed_at: datetime | None


class ActivityRow(BaseModel):
    search_result_id: int
    url: str
    title: str | None
    status: str
    fetch_strategy: str | None
    query_text: str


class CampaignStatusResponse(BaseModel):
    id: int
    name: str
    status: str
    current_stage: str | None
    industries: list[str]
    locations: list[str]
    negative_locations: list[str]
    platforms: list[str]
    platform: str
    serper_credits_used: int
    max_credits_serper: int
    firecrawl_credits_used: int
    max_credits_firecrawl: int
    queries_count: int
    search_results_count: int
    leads_count: int
    scored_leads_count: int
    created_at: datetime
    completed_at: datetime | None


LeadStatus = Literal["pending", "approved", "rejected", "joined", "contacted", "archived"]
LEAD_STATUSES: tuple[LeadStatus, ...] = (
    "pending", "approved", "rejected", "joined", "contacted", "archived",
)


class TagResponse(BaseModel):
    id: int
    name: str
    color: str | None = None
    leads_count: int | None = None


class LeadResponse(BaseModel):
    id: int
    invite_id: str
    source_url: str | None
    relevance: int | None
    geo_fit: int | None
    engagement: int | None
    total_score: int | None
    status: LeadStatus
    tags: list[str] = []
    verified_group_name: str | None
    verified_group_description: str | None
    last_validated_at: datetime | None
    first_seen: datetime
    last_seen: datetime

    class Config:
        from_attributes = True


class UpdateLeadStatusRequest(BaseModel):
    status: LeadStatus


class StatusCounts(BaseModel):
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    joined: int = 0
    contacted: int = 0
    archived: int = 0


class FilterRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=500)
    only_scored: bool = True
    # Scope the categorization to leads in a single lifecycle stage. Default is
    # 'pending' — that's the most common use (triage fresh leads). None = all.
    status: LeadStatus | None = "pending"


class FilteredLead(BaseModel):
    id: int
    invite_id: str
    source_url: str | None
    relevance: int | None
    geo_fit: int | None
    engagement: int | None
    total_score: int | None
    reason: str
    group_name: str | None  # verified live from WhatsApp during validity check


class FilterResponse(BaseModel):
    prompt: str
    total_considered: int
    matched_count: int
    dropped_invalid: int  # LLM-matched but invite link is dead/revoked
    leads: list[FilteredLead]


# ---------- Endpoints ----------


@app.get("/campaigns", response_model=list[CampaignSummary])
async def list_campaigns(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """Most recent campaigns first, with denormalized lead counts so the dashboard
    table is one round-trip."""
    leads_count_subq = (
        select(Lead.campaign_id, func.count(Lead.id).label("n"))
        .group_by(Lead.campaign_id)
        .subquery()
    )
    scored_count_subq = (
        select(Lead.campaign_id, func.count(Lead.id).label("n"))
        .where(Lead.last_scored_at.isnot(None))
        .group_by(Lead.campaign_id)
        .subquery()
    )

    rows = (
        await session.execute(
            select(
                Campaign,
                func.coalesce(leads_count_subq.c.n, 0).label("leads_count"),
                func.coalesce(scored_count_subq.c.n, 0).label("scored_leads_count"),
            )
            .outerjoin(leads_count_subq, leads_count_subq.c.campaign_id == Campaign.id)
            .outerjoin(scored_count_subq, scored_count_subq.c.campaign_id == Campaign.id)
            .order_by(desc(Campaign.created_at))
            .limit(limit)
        )
    ).all()

    return [
        CampaignSummary(
            id=c.id,
            name=c.name,
            status=c.status,
            current_stage=c.current_stage,
            platform=c.platform,
            leads_count=lc,
            scored_leads_count=sc,
            serper_credits_used=c.serper_credits_used,
            firecrawl_credits_used=c.firecrawl_credits_used,
            created_at=c.created_at,
            completed_at=c.completed_at,
        )
        for (c, lc, sc) in rows
    ]


@app.post("/campaigns", status_code=201, response_model=CreateCampaignResponse)
async def create_campaign(
    req: CreateCampaignRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    c = Campaign(
        name=req.name,
        industries=req.industries,
        locations=req.locations,
        negative_locations=req.negative_locations,
        platforms=req.platforms,
        platform=req.platform,
        max_credits_serper=req.max_credits_serper,
        max_credits_firecrawl=req.max_credits_firecrawl,
    )
    session.add(c)
    await session.commit()
    await app.state.arq_pool.enqueue_job("run_campaign", c.id)
    logger.info("campaign %s enqueued: %s", c.id, c.name)
    return CreateCampaignResponse(id=c.id, status=c.status)


@app.get("/campaigns/{campaign_id}", response_model=CampaignStatusResponse)
async def get_campaign(
    campaign_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    queries_count = await session.scalar(
        select(func.count(QueryModel.id)).where(QueryModel.campaign_id == campaign_id)
    )
    sr_count = await session.scalar(
        select(func.count(SearchResult.id))
        .join(QueryModel, SearchResult.query_id == QueryModel.id)
        .where(QueryModel.campaign_id == campaign_id)
    )
    leads_count = await session.scalar(
        select(func.count(Lead.id)).where(Lead.campaign_id == campaign_id)
    )
    scored_count = await session.scalar(
        select(func.count(Lead.id)).where(
            Lead.campaign_id == campaign_id,
            Lead.last_scored_at.isnot(None),
        )
    )

    return CampaignStatusResponse(
        id=c.id,
        name=c.name,
        status=c.status,
        current_stage=c.current_stage,
        industries=c.industries,
        locations=c.locations,
        negative_locations=c.negative_locations,
        platforms=c.platforms,
        platform=c.platform,
        serper_credits_used=c.serper_credits_used,
        max_credits_serper=c.max_credits_serper,
        firecrawl_credits_used=c.firecrawl_credits_used,
        max_credits_firecrawl=c.max_credits_firecrawl,
        queries_count=queries_count or 0,
        search_results_count=sr_count or 0,
        leads_count=leads_count or 0,
        scored_leads_count=scored_count or 0,
        created_at=c.created_at,
        completed_at=c.completed_at,
    )


@app.get("/campaigns/{campaign_id}/activity", response_model=list[ActivityRow])
async def campaign_activity(
    campaign_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Most recent search_results for a campaign — drives the live 'currently
    fetching' feed in the dashboard. Ordered by id desc (newer-first)."""
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    rows = (
        await session.execute(
            select(
                SearchResult.id,
                SearchResult.url,
                SearchResult.title,
                SearchResult.status,
                SearchResult.fetch_strategy,
                QueryModel.query_text,
            )
            .join(QueryModel, SearchResult.query_id == QueryModel.id)
            .where(QueryModel.campaign_id == campaign_id)
            .order_by(desc(SearchResult.id))
            .limit(limit)
        )
    ).all()
    return [
        ActivityRow(
            search_result_id=row.id,
            url=row.url,
            title=row.title,
            status=row.status,
            fetch_strategy=row.fetch_strategy,
            query_text=row.query_text,
        )
        for row in rows
    ]


async def _hydrate_leads_with_tags(
    session: AsyncSession, leads: list[Lead]
) -> list[LeadResponse]:
    """Fan out to lead_tags+tags once and stitch tag names back onto each lead."""
    if not leads:
        return []
    lead_ids = [ld.id for ld in leads]
    rows = (
        await session.execute(
            select(LeadTag.lead_id, Tag.name)
            .join(Tag, Tag.id == LeadTag.tag_id)
            .where(LeadTag.lead_id.in_(lead_ids))
            .order_by(Tag.name)
        )
    ).all()
    by_lead: dict[int, list[str]] = {}
    for lead_id, name in rows:
        by_lead.setdefault(lead_id, []).append(name)
    out: list[LeadResponse] = []
    for ld in leads:
        resp = LeadResponse.model_validate(ld)
        resp.tags = by_lead.get(ld.id, [])
        out.append(resp)
    return out


@app.get("/campaigns/{campaign_id}/leads", response_model=list[LeadResponse])
async def list_leads(
    campaign_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    only_scored: bool = False,
    status: LeadStatus | None = None,
    tag_id: Annotated[list[int] | None, Query()] = None,
):
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    stmt = select(Lead).where(Lead.campaign_id == campaign_id)
    if only_scored:
        stmt = stmt.where(Lead.last_scored_at.isnot(None))
    if status is not None:
        stmt = stmt.where(Lead.status == status)
    if tag_id:
        # OR semantics: leads carrying any of the given tags.
        stmt = stmt.where(
            Lead.id.in_(
                select(LeadTag.lead_id).where(LeadTag.tag_id.in_(tag_id))
            )
        )
    stmt = stmt.order_by(desc(Lead.total_score), desc(Lead.first_seen)).limit(limit).offset(offset)

    leads = list((await session.execute(stmt)).scalars())
    return await _hydrate_leads_with_tags(session, leads)


@app.get("/campaigns/{campaign_id}/tags", response_model=list[TagResponse])
async def list_tags(
    campaign_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """All tags for a campaign with per-tag usage counts (drives the filter chips)."""
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    rows = (
        await session.execute(
            select(Tag.id, Tag.name, Tag.color, func.count(LeadTag.lead_id))
            .outerjoin(LeadTag, LeadTag.tag_id == Tag.id)
            .where(Tag.campaign_id == campaign_id)
            .group_by(Tag.id)
            .order_by(Tag.name)
        )
    ).all()
    return [
        TagResponse(id=r[0], name=r[1], color=r[2], leads_count=r[3])
        for r in rows
    ]


class AutoTagResponse(BaseModel):
    considered: int
    tagged: int
    tags_created: int
    links_created: int
    business_attached: int = 0
    business_detached: int = 0
    misc_attached: int = 0
    misc_detached: int = 0


class ExportFilter(BaseModel):
    statuses: list[LeadStatus] | None = None  # None = any status
    tag_ids: list[int] | None = None  # OR semantics across tags
    min_total_score: int | None = None
    max_total_score: int | None = None
    only_scored: bool = False
    only_valid_invites: bool = False  # cross-check against the validity cache


@app.post("/campaigns/{campaign_id}/export.csv")
async def export_leads_csv(
    campaign_id: int,
    filt: ExportFilter,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Streamed CSV of leads matching the supplied filters. Filter dimensions:
    statuses, tag_ids (OR), score range, only_scored, only_valid_invites.
    The verified group_name (when known) and tag list are included as columns."""
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    stmt = select(Lead).where(Lead.campaign_id == campaign_id)
    if filt.statuses:
        stmt = stmt.where(Lead.status.in_(filt.statuses))
    if filt.only_scored:
        stmt = stmt.where(Lead.last_scored_at.isnot(None))
    if filt.min_total_score is not None:
        stmt = stmt.where(Lead.total_score >= filt.min_total_score)
    if filt.max_total_score is not None:
        stmt = stmt.where(Lead.total_score <= filt.max_total_score)
    if filt.tag_ids:
        stmt = stmt.where(
            Lead.id.in_(select(LeadTag.lead_id).where(LeadTag.tag_id.in_(filt.tag_ids)))
        )
    stmt = stmt.order_by(desc(Lead.total_score), desc(Lead.first_seen))
    leads = list((await session.execute(stmt)).scalars())

    tag_rows = (
        await session.execute(
            select(LeadTag.lead_id, Tag.name)
            .join(Tag, Tag.id == LeadTag.tag_id)
            .where(LeadTag.lead_id.in_([ld.id for ld in leads] or [0]))
            .order_by(Tag.name)
        )
    ).all()
    tags_by_lead: dict[int, list[str]] = {}
    for lead_id, name in tag_rows:
        tags_by_lead.setdefault(lead_id, []).append(name)

    # Validity now lives on the Lead row (set by the enrichment endpoint).
    # We never hit WhatsApp from inside the export path — that's a cheap
    # filter on already-persisted data.
    if filt.only_valid_invites:
        leads = [ld for ld in leads if ld.verified_group_name is not None]

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "rank", "group_name", "group_description", "status", "tags", "total_score",
        "relevance", "geo_fit", "engagement",
        "invite_link", "source_url", "invite_valid",
    ])
    for i, ld in enumerate(leads, 1):
        if ld.last_validated_at is None:
            invite_status = "unknown"
        elif ld.verified_group_name is None:
            invite_status = "no"
        else:
            invite_status = "yes"
        w.writerow([
            i,
            ld.verified_group_name or "",
            (ld.verified_group_description or "").replace("\n", " ").replace("\r", " "),
            ld.status,
            ",".join(tags_by_lead.get(ld.id, [])),
            ld.total_score if ld.total_score is not None else "",
            ld.relevance if ld.relevance is not None else "",
            ld.geo_fit if ld.geo_fit is not None else "",
            ld.engagement if ld.engagement is not None else "",
            invite_url_for(c.platform, ld.invite_id),
            ld.source_url or "",
            invite_status,
        ])
    buf.seek(0)

    fname_safe = "".join(ch for ch in c.name if ch.isalnum() or ch in "-_") or "campaign"
    filename = f"{fname_safe}_leads.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers=headers)


class EnrichmentResponse(BaseModel):
    considered: int
    fetched: int
    valid: int
    invalid: int
    transient_errors: int


@app.post("/campaigns/{campaign_id}/enrich-whatsapp", response_model=EnrichmentResponse)
async def run_enrichment(
    campaign_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    only_unvalidated: bool = True,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
    limit: int = Query(10, ge=1, le=10, description="Max leads to enrich this call (1-10)"),
):
    """Validate the top-N leads against the campaign's platform and persist
    verified group/server name + description. Dispatches by `campaign.platform`:
      - whatsapp → hits Meta's invite landing page (rate-limited, may CAPTCHA)
      - discord  → hits Discord's public invite API (no auth, fast)

    URL kept as `/enrich-whatsapp` for back-compat with the existing frontend
    button; the path is misleading now but renaming would break in-flight
    deployments. Body shape is identical across platforms.

    Returns 503 if WA serves a CAPTCHA mid-batch, 400 if the run exceeds
    request_budget.
    """
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    if c.platform == "discord":
        from src.pipeline.discord.enricher import (
            DiscordBudgetExceeded,
            enrich_campaign as enrich_discord,
        )
        try:
            counts = await enrich_discord(
                campaign_id,
                only_unvalidated=only_unvalidated,
                request_budget=request_budget,
                limit=limit,
            )
        except DiscordBudgetExceeded as e:
            raise HTTPException(400, str(e))
        return EnrichmentResponse(
            considered=counts.considered,
            fetched=counts.fetched,
            valid=counts.valid,
            invalid=counts.invalid,
            transient_errors=counts.transient_errors,
        )

    # Default: WhatsApp.
    try:
        counts: EnrichmentCounts = await enrich_campaign(
            campaign_id,
            request.app.state.redis,
            only_unvalidated=only_unvalidated,
            request_budget=request_budget,
            limit=limit,
        )
    except WhatsAppBudgetExceeded as e:
        raise HTTPException(400, str(e))
    except WhatsAppBlocked as e:
        # 503 because the underlying upstream is rejecting us; retrying after
        # a cooldown (or via a different egress IP) is the resolution.
        raise HTTPException(503, f"WhatsApp returned a block/challenge — aborted: {e}")
    return EnrichmentResponse(
        considered=counts.considered,
        fetched=counts.fetched,
        valid=counts.valid,
        invalid=counts.invalid,
        transient_errors=counts.transient_errors,
    )


class SnowballResponse(BaseModel):
    new_queries: int
    enqueued: bool


class SubredditDiscoveryResponse(BaseModel):
    subs_mined: int
    new_search_results: int
    skipped_existing: int
    errors: int
    enqueued: bool


@app.post(
    "/campaigns/{campaign_id}/discover-subreddits",
    response_model=SubredditDiscoveryResponse,
)
async def run_subreddit_discovery(
    campaign_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Identify subreddits that already produced ≥1 lead in this campaign,
    then mine each one via Reddit's native search for additional invite-
    bearing threads. New SearchResult rows land tagged `fetch_strategy=reddit`
    `status=new` so the orchestrator's stage 4a picks them up on the next
    run. If any new rows landed, the campaign is reopened and re-enqueued."""
    from src.pipeline.shared.subreddit_discovery import discover_via_productive_subreddits

    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    counts = await discover_via_productive_subreddits(campaign_id)

    enqueued = False
    if counts["new_search_results"] > 0:
        if c.status in ("done", "budget_exceeded"):
            c.status = "running"
            c.completed_at = None
            await session.commit()
        await request.app.state.arq_pool.enqueue_job("run_campaign", campaign_id)
        enqueued = True

    return SubredditDiscoveryResponse(**counts, enqueued=enqueued)


@app.post("/campaigns/{campaign_id}/snowball", response_model=SnowballResponse)
async def run_snowball(
    campaign_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    top_n: int = 10,
):
    """Take the top-N enriched leads (highest total_score with a verified
    group/server name) and feed those names back into the query generator as
    new quoted-phrase seeds. The new queries land as `pending`; if any were
    added, we re-enqueue `run_campaign` so the orchestrator picks them up.
    The pipeline is idempotent, so existing leads aren't reprocessed.

    Dispatches to the campaign's platform: WhatsApp uses chat.whatsapp.com
    anchors, Discord uses discord.gg anchors. Both go through the same
    1B combinator under the hood."""
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    if c.platform == "discord":
        from src.pipeline.discord.queries import snowball_from_verified_names
    else:
        from src.pipeline.whatsapp.queries import snowball_from_verified_names

    new_queries = await snowball_from_verified_names(campaign_id, top_n=top_n)

    enqueued = False
    if new_queries > 0:
        # Reopen the campaign if it had finished, then re-enqueue. The
        # orchestrator's per-stage idempotency lets us replay safely.
        if c.status in ("done", "budget_exceeded"):
            c.status = "running"
            c.completed_at = None
            await session.commit()
        await request.app.state.arq_pool.enqueue_job("run_campaign", campaign_id)
        enqueued = True

    return SnowballResponse(new_queries=new_queries, enqueued=enqueued)


@app.post("/campaigns/{campaign_id}/auto-tag", response_model=AutoTagResponse)
async def run_auto_tag(
    campaign_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    only_untagged: bool = True,
    limit: int = Query(10, ge=1, le=10, description="Max leads to tag this call (1-10)"),
):
    """LLM pass that assigns 1-3 short categorical tags per scored lead. By default
    runs only on leads that have no tags yet; pass only_untagged=false to retag all.
    Processes at most `limit` leads (1-10) per call, highest total_score first."""
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    counts = await auto_tag_campaign(campaign_id, only_untagged=only_untagged, limit=limit)
    return AutoTagResponse(**counts)


@app.get("/campaigns/{campaign_id}/leads/status-counts", response_model=StatusCounts)
async def status_counts(
    campaign_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Per-status lead counts; drives the filter tabs."""
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    rows = (
        await session.execute(
            select(Lead.status, func.count(Lead.id))
            .where(Lead.campaign_id == campaign_id)
            .group_by(Lead.status)
        )
    ).all()
    counts = StatusCounts()
    for status_val, n in rows:
        if hasattr(counts, status_val):
            setattr(counts, status_val, n)
    return counts


class DeleteDeadResponse(BaseModel):
    deleted: int


@app.delete("/campaigns/{campaign_id}/leads/dead", response_model=DeleteDeadResponse)
async def delete_dead_leads(
    campaign_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Bulk-delete leads whose invite has been validated AND came back dead.
    lead_tags rows cascade away via FK ON DELETE CASCADE."""
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    result = await session.execute(
        delete(Lead).where(
            Lead.campaign_id == campaign_id,
            Lead.last_validated_at.isnot(None),
            Lead.verified_group_name.is_(None),
        )
    )
    await session.commit()
    return DeleteDeadResponse(deleted=result.rowcount or 0)


@app.patch("/campaigns/{campaign_id}/leads/{lead_id}/status", response_model=LeadResponse)
async def update_lead_status(
    campaign_id: int,
    lead_id: int,
    req: UpdateLeadStatusRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Move a lead through its lifecycle (Module 3 approval + Module 4 organizer)."""
    ld = await session.get(Lead, lead_id)
    if ld is None or ld.campaign_id != campaign_id:
        raise HTTPException(404, f"lead {lead_id} not found in campaign {campaign_id}")
    ld.status = req.status
    await session.commit()
    await session.refresh(ld)
    return LeadResponse.model_validate(ld)


@app.post("/campaigns/{campaign_id}/filter", response_model=FilterResponse)
async def filter_leads_by_prompt(
    campaign_id: int,
    req: FilterRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """LLM-classify this campaign's leads against a free-form category prompt
    (e.g. "AI groups", "Dubai-based business groups"), then verify each match
    against live WhatsApp to drop dead/revoked invite links. Sync — typical
    end-to-end ~5-15s for 50 leads on a cold cache, sub-second once cached."""
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    stmt = select(Lead).where(Lead.campaign_id == campaign_id)
    if req.only_scored:
        stmt = stmt.where(Lead.last_scored_at.isnot(None))
    if req.status is not None:
        stmt = stmt.where(Lead.status == req.status)
    stmt = stmt.order_by(desc(Lead.total_score), desc(Lead.first_seen))
    leads = list((await session.execute(stmt)).scalars())

    decisions = await filter_leads(req.prompt, leads)

    candidates: list[tuple[Lead, str]] = []
    for ld in leads:
        d = decisions.get(ld.invite_id)
        if d is None or not d.matches:
            continue
        candidates.append((ld, d.reason))

    matched: list[FilteredLead] = []
    dropped = 0
    for ld, reason in candidates:
        # Drop only leads we *know* are dead (validation has run AND came
        # back invalid). Leads that haven't been enriched yet are kept so
        # we don't silently hide them — run "Enrich WhatsApp" for clean drop.
        if ld.last_validated_at is not None and ld.verified_group_name is None:
            dropped += 1
            continue
        matched.append(
            FilteredLead(
                id=ld.id,
                invite_id=ld.invite_id,
                source_url=ld.source_url,
                relevance=ld.relevance,
                geo_fit=ld.geo_fit,
                engagement=ld.engagement,
                total_score=ld.total_score,
                reason=reason,
                group_name=ld.verified_group_name,
            )
        )

    return FilterResponse(
        prompt=req.prompt,
        total_considered=len(leads),
        matched_count=len(matched),
        dropped_invalid=dropped,
        leads=matched,
    )


# ---------- Module 7: WhatsApp numbers ----------


WaStatus = Literal[
    "pending",
    "qr_pending",
    "connecting",
    "connected",
    "disconnected",
    "logged_out",
]


class CreateNumberRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)


class WhatsAppNumberResponse(BaseModel):
    id: int
    display_name: str
    msisdn: str | None
    session_id: str
    status: WaStatus
    qr_data_url: str | None  # populated only while status == 'qr_pending'
    last_seen_at: datetime | None
    created_at: datetime


def _wa_response(
    row: WhatsAppNumber, qr_data_url: str | None = None
) -> WhatsAppNumberResponse:
    return WhatsAppNumberResponse(
        id=row.id,
        display_name=row.display_name,
        msisdn=row.msisdn,
        session_id=row.session_id,
        status=row.status,  # type: ignore[arg-type]
        qr_data_url=qr_data_url,
        last_seen_at=row.last_seen_at,
        created_at=row.created_at,
    )


async def _refresh_from_sidecar(
    session: AsyncSession, row: WhatsAppNumber
) -> tuple[WhatsAppNumber, str | None]:
    """Fetch current state from the sidecar and persist any drift back to the
    DB. Returns the (possibly mutated) row plus the live QR data URL (None
    unless the session is currently waiting for a scan)."""
    try:
        sc = await wa_sidecar_client.get_session(row.session_id)
    except wa_sidecar_client.WaSidecarError as e:
        logger.warning("sidecar refresh failed for session %s: %s", row.session_id, e)
        return row, None

    if sc is None:
        # Sidecar lost the session in memory (e.g. crash, container rebuild).
        # We mark the row disconnected so the user can re-initiate. The DB
        # row is the source of truth for "this number was once added".
        if row.status != "disconnected":
            row.status = "disconnected"
            await session.commit()
            await session.refresh(row)
        return row, None

    changed = (sc.status and sc.status != row.status) or (
        sc.msisdn and sc.msisdn != row.msisdn
    )
    if not changed:
        return row, sc.qr_data_url

    # Server-side now() for last_seen_at avoids clock skew between containers.
    if sc.status == "connected":
        await session.execute(
            text(
                "UPDATE whatsapp_numbers "
                "SET status=:s, msisdn=:m, last_seen_at=now() WHERE id=:id"
            ),
            {"s": sc.status, "m": sc.msisdn, "id": row.id},
        )
    else:
        await session.execute(
            text(
                "UPDATE whatsapp_numbers SET status=:s, msisdn=:m WHERE id=:id"
            ),
            {"s": sc.status, "m": sc.msisdn, "id": row.id},
        )
    await session.commit()
    await session.refresh(row)
    return row, sc.qr_data_url


@app.post("/numbers", status_code=201, response_model=WhatsAppNumberResponse)
async def create_number(
    req: CreateNumberRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Initialize a new WhatsApp connection. Persists a numbers row, asks the
    sidecar to start a Baileys session, and returns the initial QR data URL
    (poll GET /numbers/{id} for updates as the user scans)."""
    import uuid

    session_id = uuid.uuid4().hex
    row = WhatsAppNumber(
        display_name=req.display_name.strip(),
        session_id=session_id,
        status="pending",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    try:
        sc = await wa_sidecar_client.create_session(session_id, row.display_name)
    except wa_sidecar_client.WaSidecarError as e:
        # Roll back the row we just inserted — without a sidecar session it
        # has no purpose and would be a phantom in the UI.
        await session.delete(row)
        await session.commit()
        raise HTTPException(503, f"wa-sidecar error: {e}") from e

    if sc.status and sc.status != row.status:
        row.status = sc.status
        await session.commit()
        await session.refresh(row)

    return _wa_response(row, qr_data_url=sc.qr_data_url)


@app.get("/numbers", response_model=list[WhatsAppNumberResponse])
async def list_numbers(
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """List all linked numbers with their *current* status. Reconciles each
    row against the sidecar in case the WS state drifted (reconnect, logout)."""
    rows = list(
        (
            await session.execute(
                select(WhatsAppNumber).order_by(desc(WhatsAppNumber.created_at))
            )
        ).scalars()
    )
    out: list[WhatsAppNumberResponse] = []
    for row in rows:
        refreshed, qr = await _refresh_from_sidecar(session, row)
        out.append(_wa_response(refreshed, qr_data_url=qr))
    return out


@app.get("/numbers/{number_id}", response_model=WhatsAppNumberResponse)
async def get_number(
    number_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Single-number detail — used by the AddNumberModal poll loop while the
    user is scanning the QR. Returns fresh state from the sidecar."""
    row = await session.get(WhatsAppNumber, number_id)
    if row is None:
        raise HTTPException(404, f"number {number_id} not found")
    refreshed, qr = await _refresh_from_sidecar(session, row)
    return _wa_response(refreshed, qr_data_url=qr)


@app.delete("/numbers/{number_id}", status_code=200)
async def delete_number(
    number_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Logout + remove a linked number. Best-effort: we always delete the
    DB row even if the sidecar logout fails (so the UI can recover from
    drifted state)."""
    row = await session.get(WhatsAppNumber, number_id)
    if row is None:
        raise HTTPException(404, f"number {number_id} not found")
    try:
        await wa_sidecar_client.logout_session(row.session_id)
    except wa_sidecar_client.WaSidecarError as e:
        logger.warning("sidecar logout failed for %s: %s", row.session_id, e)
    await session.delete(row)
    await session.commit()
    return {"ok": True}


# ---------- Module 5: groups + broadcast ----------


class GroupResponse(BaseModel):
    jid: str
    subject: str
    participants_count: int
    announce: bool  # admins-only — sending will fail unless we're admin


@app.get("/numbers/{number_id}/groups", response_model=list[GroupResponse])
async def list_groups_for_number(
    number_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """All WhatsApp groups this number is a member of, fetched fresh from the
    sidecar (which queries Baileys' in-memory store). Returns 409 if the
    primary phone has dropped offline — the caller should reconnect first."""
    row = await session.get(WhatsAppNumber, number_id)
    if row is None:
        raise HTTPException(404, f"number {number_id} not found")
    try:
        groups = await wa_sidecar_client.list_groups(row.session_id)
    except wa_sidecar_client.NotConnected as e:
        raise HTTPException(409, str(e)) from e
    except wa_sidecar_client.WaSidecarError as e:
        raise HTTPException(503, f"wa-sidecar error: {e}") from e
    return [
        GroupResponse(
            jid=g.jid,
            subject=g.subject,
            participants_count=g.participants_count,
            announce=g.announce,
        )
        for g in groups
    ]


class BroadcastRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4096)
    targets: list[str] = Field(min_length=1, max_length=200)
    min_delay_seconds: int = Field(default=3, ge=0, le=600)
    max_delay_seconds: int = Field(default=15, ge=0, le=600)


class BroadcastJobResponse(BaseModel):
    id: int
    number_id: int
    body: str
    status: str
    total_targets: int
    sent_count: int
    failed_count: int
    min_delay_seconds: int
    max_delay_seconds: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class OutboundMessageResponse(BaseModel):
    id: int
    jid: str
    group_subject: str | None
    status: str
    error: str | None
    attempted_at: datetime


class BroadcastDetailResponse(BroadcastJobResponse):
    messages: list[OutboundMessageResponse]


def _broadcast_job_view(job: BroadcastJob) -> BroadcastJobResponse:
    return BroadcastJobResponse(
        id=job.id,
        number_id=job.number_id,
        body=job.body,
        status=job.status,
        total_targets=job.total_targets,
        sent_count=job.sent_count,
        failed_count=job.failed_count,
        min_delay_seconds=job.min_delay_seconds,
        max_delay_seconds=job.max_delay_seconds,
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


@app.post(
    "/numbers/{number_id}/broadcast",
    status_code=201,
    response_model=BroadcastJobResponse,
)
async def create_broadcast(
    number_id: int,
    req: BroadcastRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Enqueue a throttled bulk-send. Validates targets + creates per-message
    rows up front so the worker has everything it needs to crash-resume."""
    if req.max_delay_seconds < req.min_delay_seconds:
        raise HTTPException(400, "max_delay_seconds must be >= min_delay_seconds")

    number = await session.get(WhatsAppNumber, number_id)
    if number is None:
        raise HTTPException(404, f"number {number_id} not found")
    if number.status != "connected":
        raise HTTPException(
            409, f"number is {number.status}; reconnect before broadcasting"
        )

    # Resolve subjects up front so the audit log carries them — the source of
    # truth (sidecar's group cache) can drift later. JIDs that the number isn't
    # actually a member of are dropped with a 400.
    try:
        groups = await wa_sidecar_client.list_groups(number.session_id)
    except wa_sidecar_client.NotConnected as e:
        raise HTTPException(409, str(e)) from e
    except wa_sidecar_client.WaSidecarError as e:
        raise HTTPException(503, f"wa-sidecar error: {e}") from e
    subject_by_jid = {g.jid: g.subject for g in groups}
    missing = [j for j in req.targets if j not in subject_by_jid]
    if missing:
        raise HTTPException(
            400,
            f"these JIDs aren't groups this number is in: {missing[:5]}",
        )

    job = BroadcastJob(
        number_id=number.id,
        body=req.body,
        total_targets=len(req.targets),
        min_delay_seconds=req.min_delay_seconds,
        max_delay_seconds=req.max_delay_seconds,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    for jid in req.targets:
        session.add(
            OutboundMessage(
                job_id=job.id,
                jid=jid,
                group_subject=subject_by_jid.get(jid),
                status="queued",
            )
        )
    await session.commit()

    await app.state.arq_pool.enqueue_job("run_broadcast", job.id)
    logger.info("broadcast %s enqueued (number=%s targets=%d)", job.id, number.id, len(req.targets))
    return _broadcast_job_view(job)


@app.get(
    "/numbers/{number_id}/broadcasts",
    response_model=list[BroadcastJobResponse],
)
async def list_broadcasts(
    number_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    rows = list(
        (
            await session.execute(
                select(BroadcastJob)
                .where(BroadcastJob.number_id == number_id)
                .order_by(desc(BroadcastJob.created_at))
                .limit(limit)
            )
        ).scalars()
    )
    return [_broadcast_job_view(r) for r in rows]


class BroadcastSummaryResponse(BroadcastJobResponse):
    """List-row variant — same fields as BroadcastJobResponse plus the owning
    number's display_name + msisdn so the UI can render context without a
    second round-trip per row."""

    number_display_name: str
    number_msisdn: str | None


@app.get("/broadcasts", response_model=list[BroadcastSummaryResponse])
async def list_all_broadcasts(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    status: str | None = None,
):
    """All broadcast jobs across every number. Newest first. Filter by status
    if provided. Drives the /broadcasts page."""
    stmt = (
        select(BroadcastJob, WhatsAppNumber.display_name, WhatsAppNumber.msisdn)
        .join(WhatsAppNumber, WhatsAppNumber.id == BroadcastJob.number_id)
    )
    if status:
        stmt = stmt.where(BroadcastJob.status == status)
    stmt = stmt.order_by(desc(BroadcastJob.created_at)).limit(limit)
    rows = list((await session.execute(stmt)).all())
    return [
        BroadcastSummaryResponse(
            **_broadcast_job_view(job).model_dump(),
            number_display_name=display_name,
            number_msisdn=msisdn,
        )
        for job, display_name, msisdn in rows
    ]


@app.get("/broadcasts/{job_id}", response_model=BroadcastDetailResponse)
async def get_broadcast(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    job = await session.get(BroadcastJob, job_id)
    if job is None:
        raise HTTPException(404, f"broadcast {job_id} not found")
    msgs = list(
        (
            await session.execute(
                select(OutboundMessage)
                .where(OutboundMessage.job_id == job_id)
                .order_by(OutboundMessage.id)
            )
        ).scalars()
    )
    return BroadcastDetailResponse(
        **_broadcast_job_view(job).model_dump(),
        messages=[
            OutboundMessageResponse(
                id=m.id,
                jid=m.jid,
                group_subject=m.group_subject,
                status=m.status,
                error=m.error,
                attempted_at=m.attempted_at,
            )
            for m in msgs
        ],
    )


# ---------- Module 6: Shared inbox ----------


async def _require_internal_token(
    x_internal_token: Annotated[str | None, Header()] = None,
):
    """Gate for sidecar → api webhooks. We don't expose port 3001 outside the
    Docker network, but port 8000 IS exposed for the browser, so this header
    check is the actual security boundary."""
    if not settings.wa_sidecar_secret:
        raise HTTPException(503, "internal webhook disabled (no secret configured)")
    if x_internal_token != settings.wa_sidecar_secret:
        raise HTTPException(401, "invalid internal token")


class InboundMessageRequest(BaseModel):
    session_id: str
    wa_message_id: str
    chat_jid: str
    # Group subject for kind='group'; contact push name for kind='dm'. None if
    # the sidecar couldn't resolve it (we'll keep whatever conversation.name
    # we already had, or fall back to the jid).
    chat_name: str | None = None
    kind: Literal["dm", "group"]
    direction: Literal["in", "out"]
    sender_jid: str | None
    # WhatsApp pushName of THIS message's author. None for outbound from us.
    sender_name: str | None = None
    body: str = ""
    ts_unix: int


@app.post("/internal/inbound", status_code=204)
async def inbound_message(
    payload: InboundMessageRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[None, Depends(_require_internal_token)] = None,
):
    """Sidecar webhook for new messages. Idempotent — duplicate webhooks for
    the same wa_message_id are dropped via the unique constraint on
    (conversation_id, wa_message_id)."""
    number = await session.scalar(
        select(WhatsAppNumber).where(WhatsAppNumber.session_id == payload.session_id)
    )
    if number is None:
        # Sidecar might race ahead of the api row creation in rare cases; just
        # drop the message rather than error out.
        logger.info("inbound for unknown session %s — dropped", payload.session_id)
        return None

    # Upsert conversation. We use number_id+jid as the natural key.
    conv = await session.scalar(
        select(Conversation).where(
            Conversation.number_id == number.id,
            Conversation.jid == payload.chat_jid,
        )
    )
    ts = datetime.fromtimestamp(payload.ts_unix, tz=timezone.utc)
    # For groups, prefix preview with sender name (WA-Web style: "Alice: hi"
    # so the user sees who said what without opening the thread).
    if payload.kind == "group" and payload.direction == "in" and payload.sender_name:
        preview = f"{payload.sender_name}: {payload.body}"[:200]
    else:
        preview = payload.body[:200] if payload.body else ""
    # Best chat-name guess: explicit chat_name from sidecar > previously stored
    # conversation name > jid as last resort.
    name_hint = payload.chat_name or payload.chat_jid

    if conv is None:
        conv = Conversation(
            number_id=number.id,
            jid=payload.chat_jid,
            kind=payload.kind,
            name=name_hint,
            last_message_at=ts,
            last_message_preview=preview,
            unread_count=1 if payload.direction == "in" else 0,
        )
        session.add(conv)
        await session.flush()
    else:
        conv.last_message_at = ts
        conv.last_message_preview = preview
        if payload.direction == "in":
            conv.unread_count += 1
        # Refresh chat name when sidecar gives us a real one (replaces a stale
        # jid placeholder OR a stale push name with the now-known group subject).
        if payload.chat_name and conv.name != payload.chat_name:
            # For groups, sidecar's group subject is authoritative — always update.
            # For DMs, only promote when we currently only have the jid.
            if payload.kind == "group" or (
                not conv.name or conv.name == conv.jid
            ):
                conv.name = payload.chat_name

    msg = Message(
        conversation_id=conv.id,
        wa_message_id=payload.wa_message_id,
        direction=payload.direction,
        sender_jid=payload.sender_jid,
        sender_name=payload.sender_name,
        body=payload.body,
        ts=ts,
        status="received" if payload.direction == "in" else "sent",
    )
    try:
        session.add(msg)
        await session.commit()
    except Exception:
        # Almost certainly the unique-key collision (duplicate webhook). Roll
        # back the message but keep the conversation update we already did.
        await session.rollback()
    return None


class ConversationResponse(BaseModel):
    id: int
    number_id: int
    number_display_name: str
    jid: str
    kind: Literal["dm", "group"]
    name: str | None
    last_message_at: datetime | None
    last_message_preview: str | None
    unread_count: int
    tags: list[str] = []  # inbox_tags.name list, alphabetical


class MessageResponse(BaseModel):
    id: int
    direction: Literal["in", "out"]
    sender_jid: str | None
    sender_name: str | None
    body: str
    ts: datetime
    status: str


@app.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    session: Annotated[AsyncSession, Depends(get_session)],
    number_id: int | None = None,
    kind: Literal["dm", "group"] | None = None,
    tag_id: Annotated[list[int] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
):
    """Unified inbox feed. Default: every conversation across every connected
    number, newest message first. Filterable by number, kind, and inbox tag(s)
    (OR semantics — pass tag_id multiple times for multiple tags)."""
    stmt = (
        select(Conversation, WhatsAppNumber.display_name)
        .join(WhatsAppNumber, WhatsAppNumber.id == Conversation.number_id)
    )
    if number_id is not None:
        stmt = stmt.where(Conversation.number_id == number_id)
    if kind is not None:
        stmt = stmt.where(Conversation.kind == kind)
    if tag_id:
        stmt = stmt.where(
            Conversation.id.in_(
                select(ConversationTag.conversation_id).where(
                    ConversationTag.tag_id.in_(tag_id)
                )
            )
        )
    stmt = stmt.order_by(
        desc(Conversation.last_message_at), desc(Conversation.id)
    ).limit(limit)
    rows = list((await session.execute(stmt)).all())

    # Hydrate tags in one shot rather than N+1.
    conv_ids = [c.id for c, _ in rows]
    tags_by_conv: dict[int, list[str]] = {}
    if conv_ids:
        tag_rows = list(
            (
                await session.execute(
                    select(ConversationTag.conversation_id, InboxTag.name)
                    .join(InboxTag, InboxTag.id == ConversationTag.tag_id)
                    .where(ConversationTag.conversation_id.in_(conv_ids))
                    .order_by(InboxTag.name)
                )
            ).all()
        )
        for cid, name in tag_rows:
            tags_by_conv.setdefault(cid, []).append(name)

    return [
        ConversationResponse(
            id=c.id,
            number_id=c.number_id,
            number_display_name=display_name,
            jid=c.jid,
            kind=c.kind,  # type: ignore[arg-type]
            name=c.name,
            last_message_at=c.last_message_at,
            last_message_preview=c.last_message_preview,
            unread_count=c.unread_count,
            tags=tags_by_conv.get(c.id, []),
        )
        for c, display_name in rows
    ]


# ---------- Inbox tag CRUD + attach/detach ----------


class InboxTagResponse(BaseModel):
    id: int
    name: str
    color: str | None
    conversations_count: int


class CreateInboxTagRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    color: str | None = Field(default=None, max_length=16)


def _normalize_inbox_tag(name: str) -> str:
    """Match the lower-case + trim convention so 'AI' and 'ai' don't dupe."""
    return name.strip().lower()[:40]


@app.get("/inbox/tags", response_model=list[InboxTagResponse])
async def list_inbox_tags(
    session: Annotated[AsyncSession, Depends(get_session)],
):
    rows = (
        await session.execute(
            select(
                InboxTag.id,
                InboxTag.name,
                InboxTag.color,
                func.count(ConversationTag.conversation_id),
            )
            .outerjoin(ConversationTag, ConversationTag.tag_id == InboxTag.id)
            .group_by(InboxTag.id)
            .order_by(InboxTag.name)
        )
    ).all()
    return [
        InboxTagResponse(id=r[0], name=r[1], color=r[2], conversations_count=r[3])
        for r in rows
    ]


@app.post(
    "/inbox/tags", status_code=201, response_model=InboxTagResponse
)
async def create_inbox_tag(
    req: CreateInboxTagRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Create-or-return: if a tag with the normalized name already exists,
    return it (idempotent). Lets the frontend do create-on-the-fly UX
    without worrying about duplicate-name errors."""
    name = _normalize_inbox_tag(req.name)
    if not name:
        raise HTTPException(400, "tag name cannot be empty")
    existing = await session.scalar(
        select(InboxTag).where(InboxTag.name == name)
    )
    if existing is not None:
        return InboxTagResponse(
            id=existing.id, name=existing.name, color=existing.color, conversations_count=0
        )
    tag = InboxTag(name=name, color=req.color)
    session.add(tag)
    await session.commit()
    await session.refresh(tag)
    return InboxTagResponse(
        id=tag.id, name=tag.name, color=tag.color, conversations_count=0
    )


@app.delete("/inbox/tags/{tag_id}", status_code=204)
async def delete_inbox_tag(
    tag_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    tag = await session.get(InboxTag, tag_id)
    if tag is None:
        raise HTTPException(404, f"tag {tag_id} not found")
    await session.delete(tag)
    await session.commit()
    return None


class AttachTagRequest(BaseModel):
    tag_id: int


@app.post("/conversations/{conv_id}/tags", status_code=204)
async def attach_conversation_tag(
    conv_id: int,
    req: AttachTagRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    conv = await session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, f"conversation {conv_id} not found")
    tag = await session.get(InboxTag, req.tag_id)
    if tag is None:
        raise HTTPException(404, f"tag {req.tag_id} not found")
    # Idempotent attach via ON CONFLICT DO NOTHING.
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    await session.execute(
        pg_insert(ConversationTag)
        .values(conversation_id=conv_id, tag_id=req.tag_id)
        .on_conflict_do_nothing(index_elements=["conversation_id", "tag_id"])
    )
    await session.commit()
    return None


@app.delete(
    "/conversations/{conv_id}/tags/{tag_id}", status_code=204
)
async def detach_conversation_tag(
    conv_id: int,
    tag_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    await session.execute(
        delete(ConversationTag).where(
            ConversationTag.conversation_id == conv_id,
            ConversationTag.tag_id == tag_id,
        )
    )
    await session.commit()
    return None


@app.get(
    "/conversations/{conv_id}/messages", response_model=list[MessageResponse]
)
async def list_messages(
    conv_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
):
    conv = await session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, f"conversation {conv_id} not found")
    rows = list(
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conv_id)
                .order_by(Message.ts, Message.id)
                .limit(limit)
            )
        ).scalars()
    )
    return [
        MessageResponse(
            id=m.id,
            direction=m.direction,  # type: ignore[arg-type]
            sender_jid=m.sender_jid,
            sender_name=m.sender_name,
            body=m.body,
            ts=m.ts,
            status=m.status,
        )
        for m in rows
    ]


class RefreshNamesResponse(BaseModel):
    refreshed: int
    skipped: int


@app.post(
    "/conversations/refresh-group-names",
    response_model=RefreshNamesResponse,
)
async def refresh_group_names(
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """For every connected number, ask the sidecar for its current group list
    and overwrite stale conversation names with the real group subjects.

    Useful for backfilling rows created before the sidecar started passing
    chat_name explicitly — and as a self-heal whenever group subjects change."""
    numbers = list(
        (
            await session.execute(
                select(WhatsAppNumber).where(WhatsAppNumber.status == "connected")
            )
        ).scalars()
    )
    refreshed = 0
    skipped = 0
    for number in numbers:
        try:
            groups = await wa_sidecar_client.list_groups(number.session_id)
        except wa_sidecar_client.WaSidecarError as e:
            logger.warning("refresh-group-names: %s skipped (%s)", number.session_id, e)
            skipped += 1
            continue
        subject_by_jid = {g.jid: g.subject for g in groups if g.subject}
        if not subject_by_jid:
            continue
        rows = list(
            (
                await session.execute(
                    select(Conversation).where(
                        Conversation.number_id == number.id,
                        Conversation.kind == "group",
                        Conversation.jid.in_(list(subject_by_jid.keys())),
                    )
                )
            ).scalars()
        )
        for c in rows:
            new_name = subject_by_jid.get(c.jid)
            if new_name and c.name != new_name:
                c.name = new_name
                refreshed += 1
        await session.commit()
    return RefreshNamesResponse(refreshed=refreshed, skipped=skipped)


@app.post("/conversations/{conv_id}/read", status_code=204)
async def mark_conversation_read(
    conv_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Reset unread_count to 0. We don't tell WhatsApp the chat is read — that
    would generate read-receipt traffic and risk being interpreted as bot
    behavior. UI-side state only."""
    conv = await session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, f"conversation {conv_id} not found")
    conv.unread_count = 0
    await session.commit()
    return None


class SendReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4096)


@app.post(
    "/conversations/{conv_id}/messages",
    status_code=201,
    response_model=MessageResponse,
)
async def send_reply(
    conv_id: int,
    req: SendReplyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Send a reply via the conversation's owning number. Persists optimistically
    so the UI sees the row even if the sidecar's webhook is slow."""
    conv = await session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(404, f"conversation {conv_id} not found")
    number = await session.get(WhatsAppNumber, conv.number_id)
    if number is None:
        raise HTTPException(404, "owning number missing")
    if number.status != "connected":
        raise HTTPException(
            409, f"number is {number.status}; reconnect before sending"
        )

    try:
        sent = await wa_sidecar_client.send_message(
            number.session_id, conv.jid, req.body
        )
    except wa_sidecar_client.NotConnected as e:
        raise HTTPException(409, str(e)) from e
    except wa_sidecar_client.WaSidecarError as e:
        raise HTTPException(503, f"wa-sidecar error: {e}") from e

    now = datetime.now(timezone.utc)
    # Insert the outbound row ourselves rather than waiting on the upsert
    # webhook — the sidecar still emits one, but the unique-key conflict will
    # silently drop the duplicate. This keeps the UI snappy.
    msg = Message(
        conversation_id=conv.id,
        wa_message_id=sent.message_id or f"local-{now.timestamp()}",
        direction="out",
        sender_jid=None,
        body=req.body,
        ts=now,
        status="sent",
    )
    session.add(msg)
    conv.last_message_at = now
    conv.last_message_preview = req.body[:200]
    await session.commit()
    await session.refresh(msg)
    return MessageResponse(
        id=msg.id,
        direction="out",
        sender_jid=None,
        sender_name=None,
        body=msg.body,
        ts=msg.ts,
        status=msg.status,
    )
