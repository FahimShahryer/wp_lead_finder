from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    industries: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    locations: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    negative_locations: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    platforms: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    # Set by the orchestrator before each stage runs ('queries' / 'search' / 'prefilter' /
    # 'fetch_reddit' / 'fetch_web' / 'extract' / 'score'); cleared when terminal.
    current_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Budget guards. Step 4 added Serper; step 7 adds Firecrawl. LLM cap lands in step 9.
    max_credits_serper: Mapped[int] = mapped_column(
        Integer, nullable=False, default=500, server_default="500"
    )
    serper_credits_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_credits_firecrawl: Mapped[int] = mapped_column(
        Integer, nullable=False, default=200, server_default="200"
    )
    firecrawl_credits_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    queries: Mapped[list[Query]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )
    leads: Mapped[list[Lead]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )


class Query(Base):
    __tablename__ = "queries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", index=True)

    campaign: Mapped[Campaign] = relationship(back_populates="queries")
    search_results: Mapped[list[SearchResult]] = relationship(
        back_populates="query", cascade="all, delete-orphan", passive_deletes=True
    )


class SearchResult(Base):
    __tablename__ = "search_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    query_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("queries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new", index=True)

    # Set by stage 3: 'snippet_hit' | 'reddit' | 'web' | 'skip'.
    # NULL = stage 3 hasn't run yet on this row.
    fetch_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    query: Mapped[Query] = relationship(back_populates="search_results")

    __table_args__ = (Index("ix_search_results_url", "url"),)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Same invite_id may appear under multiple campaigns — each run analyses
    # it fresh. Uniqueness is per-campaign (see __table_args__ below).
    invite_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    detected_geo: Mapped[str | None] = mapped_column(String(8), nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)

    relevance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geo_fit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engagement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_score: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # Persisted facts pulled live from WhatsApp's invite landing page during the
    # WA-enrichment stage. NULL until enrichment runs. After enrichment:
    #   verified_group_name IS NOT NULL  → invite is live and joinable
    #   verified_group_name IS NULL AND last_validated_at IS NOT NULL → dead/expired invite
    verified_group_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    verified_group_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # Lifecycle role for the group, used by Module 3 (approval) and Module 4 (organizer).
    # 'pending' = scored but not yet vetted; 'approved' = ready for join/messaging;
    # 'rejected' = excluded; 'joined' = one of our numbers is in the group;
    # 'contacted' = a message has been sent in/about it; 'archived' = parked.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True
    )

    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )

    first_seen: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    # Bumped every time we re-encounter this invite_id during extraction (any campaign).
    # Useful for "this group is still active" / re-scoring decisions.
    last_seen: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    last_scored_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    campaign: Mapped[Campaign] = relationship(back_populates="leads")

    __table_args__ = (
        UniqueConstraint("campaign_id", "invite_id", name="uq_leads_campaign_invite"),
    )


class UrlCache(Base):
    __tablename__ = "url_cache"

    url: Mapped[str] = mapped_column(Text, primary_key=True)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Short categorical label e.g. "ai", "business", "networking". Stored
    # lowercased so auto-tag dedupes across casing variants.
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("campaign_id", "name", name="uq_tags_campaign_name"),
    )


class LeadTag(Base):
    __tablename__ = "lead_tags"

    lead_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leads.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
