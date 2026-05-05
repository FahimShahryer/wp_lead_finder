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

    # Which platform's invite links this campaign hunts for. Single-platform-
    # per-campaign by design: WhatsApp / Discord / Slack each have their own
    # query anchor, extract regex, and validator — modeling them as separate
    # campaigns keeps the pipeline auditable and isolates per-platform issues
    # from each other. Default 'whatsapp' for back-compat with legacy rows.
    platform: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="whatsapp",
        server_default="whatsapp",
        index=True,
    )

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
    # SERP/page title for source_url, captured at extraction time. Strongest
    # single discriminator for relevance scoring — a Reddit thread titled
    # "AI Agency Founders Group" tells the scorer everything in 5 words.
    source_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    detected_geo: Mapped[str | None] = mapped_column(String(8), nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)

    relevance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geo_fit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engagement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_score: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # Number of distinct source URLs that discovered this invite_id within
    # this campaign. Bumped only when a NEW (lead_id, url) pair lands in
    # `lead_source_urls`. Strong organic quality signal — invites recurring
    # across many independent sources are far more likely to be real,
    # active groups. Used as a small bonus on top of the LLM total_score.
    source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

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


class LeadSourceUrl(Base):
    """Junction table — one row per (lead, source_url) pair. Lets us count
    DISTINCT sources for an invite_id even when the same URL appears in
    multiple SearchResult rows (because it was returned by multiple queries).

    The extractor inserts with ON CONFLICT DO NOTHING; only genuinely-new
    pairs trigger a `source_count` bump on the parent Lead row.
    """

    __tablename__ = "lead_source_urls"

    lead_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("leads.id", ondelete="CASCADE"),
        primary_key=True,
    )
    url: Mapped[str] = mapped_column(Text, primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class Conversation(Base):
    """One thread per (number, chat-jid). Module 6 — Shared Inbox.

    A WhatsApp number can have many conversations: 1-on-1 DMs (jid ends with
    @s.whatsapp.net) and groups (jid ends with @g.us). The same chat seen from
    a different number is a separate Conversation row — the inbox is unified
    in the UI, but each number's view is its own thread."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    number_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("whatsapp_numbers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    jid: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default="dm", index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True, index=True
    )
    last_message_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    unread_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("number_id", "jid", name="uq_conversations_number_jid"),
    )


class Message(Base):
    """One row per WhatsApp message in a conversation. wa_message_id is the
    Baileys/WA message key id; the unique constraint makes the inbound write
    idempotent if the same event fires twice (e.g. on sidecar reconnect)."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    wa_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 'in' for messages received, 'out' for messages we sent.
    direction: Mapped[str] = mapped_column(String(4), nullable=False, index=True)
    # For DMs this equals conversations.jid; for groups it's the participant jid.
    sender_jid: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # WhatsApp 'pushName' of whoever sent the message. NULL for outbound where
    # we are the sender (rendered as 'You' on the frontend) or when the WA event
    # doesn't carry one. Cosmetic only — used to label group bubbles.
    sender_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    ts: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    # Outbound-side: queued/sent/delivered/read/failed. Inbound: received.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="received", server_default="received"
    )

    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "wa_message_id", name="uq_messages_conv_wa_id"
        ),
    )


class BroadcastJob(Base):
    """A single bulk-send request issued through one WhatsApp number. Spawns
    an arq worker that iterates the targets with random throttling and writes
    a row to outbound_messages per attempt."""

    __tablename__ = "broadcast_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    number_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("whatsapp_numbers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # queued | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued", index=True
    )
    total_targets: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    min_delay_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    max_delay_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15, server_default="15"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class OutboundMessage(Base):
    """One row per send attempt during a broadcast. status='sent' if the
    sidecar reported success; 'failed' if the call raised."""

    __tablename__ = "outbound_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("broadcast_jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    jid: Mapped[str] = mapped_column(String(80), nullable=False)
    group_subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class InboxTag(Base):
    """Global label pool for the shared inbox (Module 6 + 5).

    Distinct from `tags`/`lead_tags` (which are scoped per campaign and
    per-lead). Inbox tags span every connected number; the user creates them
    once and applies them to any conversation (DM or group). The same tags
    feed the broadcast modal's group-filter so a "AI" tag attached to N
    group conversations narrows the broadcast targets in one click.
    """

    __tablename__ = "inbox_tags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class ConversationTag(Base):
    __tablename__ = "conversation_tags"

    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("inbox_tags.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class WhatsAppNumber(Base):
    """A WhatsApp account the user has linked via QR scan. Module 7.

    The actual WS connection lives in the wa-sidecar Node.js service; this row
    persists the metadata and the sidecar-issued session_id so the api can
    look it up across restarts. msisdn is populated only after a successful
    pairing — until then it's NULL and `status` reflects QR/connecting state.
    """

    __tablename__ = "whatsapp_numbers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # User-supplied label (e.g. "Bangladesh personal", "UAE outreach").
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    # E.164-ish phone number — populated by Baileys once paired. NULL pre-scan.
    msisdn: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Sidecar's session identifier; the api proxies to /sessions/<session_id>.
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # pending | qr_pending | connecting | connected | disconnected | logged_out
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending", index=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
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
