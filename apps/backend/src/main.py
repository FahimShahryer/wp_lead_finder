import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

import redis.asyncio as redis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.db.models import Campaign, Lead, LeadTag, Tag
from src.db.models import Query as QueryModel
from src.db.models import SearchResult
from src.db.session import SessionLocal, engine, get_session
from src.pipeline.auto_tag import auto_tag_campaign
from src.pipeline.filter_by_prompt import filter_leads
from src.pipeline.wa_enricher import (
    EnrichmentCounts,
    WhatsAppBlocked,
    WhatsAppBudgetExceeded,
    enrich_campaign,
)
from src.pipeline.whatsapp_validator import DEFAULT_REQUEST_BUDGET

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

# Browser at :3000 talks to API at :8000 cross-origin during local dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
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


class CreateCampaignRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    industries: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    negative_locations: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default=["reddit", "web"])
    max_credits_serper: int = Field(default=500, ge=1)
    max_credits_firecrawl: int = Field(default=200, ge=1)


class CreateCampaignResponse(BaseModel):
    id: int
    status: str


class CampaignSummary(BaseModel):
    id: int
    name: str
    status: str
    current_stage: str | None
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
        "whatsapp_link", "source_url", "invite_valid",
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
            f"https://chat.whatsapp.com/{ld.invite_id}",
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
async def run_wa_enrichment(
    campaign_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    only_unvalidated: bool = True,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
):
    """Hit WhatsApp's invite landing page for every lead in this campaign and
    persist the verified group name + description. Skips leads already
    validated unless only_unvalidated=false. Returns 503 with a clear error
    if Meta serves a CAPTCHA / challenge mid-batch (so we don't silently
    falsify good groups), or 400 if the run exceeds request_budget."""
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    try:
        counts: EnrichmentCounts = await enrich_campaign(
            campaign_id,
            request.app.state.redis,
            only_unvalidated=only_unvalidated,
            request_budget=request_budget,
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


@app.post("/campaigns/{campaign_id}/auto-tag", response_model=AutoTagResponse)
async def run_auto_tag(
    campaign_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    only_untagged: bool = True,
):
    """LLM pass that assigns 1-3 short categorical tags per scored lead. By default
    runs only on leads that have no tags yet; pass only_untagged=false to retag all."""
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")
    counts = await auto_tag_campaign(campaign_id, only_untagged=only_untagged)
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
