import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

import redis.asyncio as redis
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.db.models import Campaign, Lead
from src.db.models import Query as QueryModel
from src.db.models import SearchResult
from src.db.session import SessionLocal, engine, get_session

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


class LeadResponse(BaseModel):
    id: int
    invite_id: str
    source_url: str | None
    relevance: int | None
    geo_fit: int | None
    engagement: int | None
    total_score: int | None
    first_seen: datetime
    last_seen: datetime

    class Config:
        from_attributes = True


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


@app.get("/campaigns/{campaign_id}/leads", response_model=list[LeadResponse])
async def list_leads(
    campaign_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    only_scored: bool = False,
):
    c = await session.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, f"campaign {campaign_id} not found")

    stmt = select(Lead).where(Lead.campaign_id == campaign_id)
    if only_scored:
        stmt = stmt.where(Lead.last_scored_at.isnot(None))
    stmt = stmt.order_by(desc(Lead.total_score), desc(Lead.first_seen)).limit(limit).offset(offset)

    rows = list((await session.execute(stmt)).scalars())
    return [LeadResponse.model_validate(r) for r in rows]
