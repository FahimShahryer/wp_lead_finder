import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db.models import Lead, LeadSourceUrl, Query, SearchResult, UrlCache
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

# `(?<![a-zA-Z0-9])` — must not be preceded by a letter/digit.
# This rejects "subchat.whatsapp.com/X" (preceded by 'b') while accepting
# "https://chat.whatsapp.com/X" (preceded by '/').
#
# `{6,30}` — invite_id length bound. Real WhatsApp invite_ids are 22 chars
# (post-2018 format); the `{6,30}` band keeps a safety buffer for any future
# format change while rejecting trivial false-positive matches like
# `chat.whatsapp.com/x` or `chat.whatsapp.com/abc` that occur in commentary,
# truncated URLs, or partial scrapes. The trailing `(?![A-Za-z0-9_-])` guard
# stops the regex consuming only the FIRST 30 chars of a longer junk string —
# without it, a 50-char malformed id would match as the first 30 of those.
_INVITE_RE = re.compile(
    r"(?<![a-zA-Z0-9])chat\.whatsapp\.com/([A-Za-z0-9_-]{6,30})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
CONTEXT_WINDOW = 200


def extract_invites(text: str | None) -> list[tuple[str, str]]:
    """Return a list of (invite_id, ±200-char context) for every invite link in text.
    Dedupes within the same text (one occurrence per invite_id, keeping first context)."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _INVITE_RE.finditer(text):
        invite_id = m.group(1)
        if invite_id in seen:
            continue
        seen.add(invite_id)
        ctx_start = max(0, m.start() - CONTEXT_WINDOW)
        ctx_end = min(len(text), m.end() + CONTEXT_WINDOW)
        out.append((invite_id, text[ctx_start:ctx_end]))
    return out


async def _upsert_lead(
    invite_id: str,
    source_url: str,
    source_title: str | None,
    source_text: str,
    campaign_id: int,
) -> bool:
    """UPSERT one lead per campaign. Returns True if a new row was inserted,
    False if a same-campaign re-discovery refreshed an existing row.

    Semantics: each campaign owns its own row per invite_id — re-running
    extraction inside a single campaign is idempotent (UPDATE source_text +
    last_seen), but the same invite_id under a different campaign is a
    fresh row, scored independently against that campaign's ICP.

    `source_title` updates only when the new value is non-empty — re-discovery
    on a snippet-only row shouldn't blank out a previously-captured title.

    Also records the (lead_id, source_url) pair in `lead_source_urls` so we
    can count distinct sources later. Multiple SearchResult rows can share
    the same URL (one per query that returned it); the junction's PK collapses
    those duplicates so source_count reflects only unique URLs.
    """
    now = datetime.now(timezone.utc)
    async with SessionLocal() as s:
        existed = await s.scalar(
            select(Lead.id).where(
                Lead.campaign_id == campaign_id,
                Lead.invite_id == invite_id,
            )
        )
        stmt = pg_insert(Lead).values(
            invite_id=invite_id,
            source_url=source_url,
            source_title=source_title,
            source_text=source_text,
            campaign_id=campaign_id,
            first_seen=now,
            last_seen=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["campaign_id", "invite_id"],
            set_={
                "source_text": stmt.excluded.source_text,
                "source_title": func.coalesce(stmt.excluded.source_title, Lead.source_title),
                "last_seen": now,
            },
        )
        await s.execute(stmt)

        # Look up the row we just upserted so we can record the source URL.
        lead_id = await s.scalar(
            select(Lead.id).where(
                Lead.campaign_id == campaign_id,
                Lead.invite_id == invite_id,
            )
        )

        url_stmt = pg_insert(LeadSourceUrl).values(lead_id=lead_id, url=source_url)
        url_stmt = url_stmt.on_conflict_do_nothing(index_elements=["lead_id", "url"])
        url_result = await s.execute(url_stmt)
        # Recompute source_count from the junction whenever a NEW pair landed.
        # Doing it via a SELECT keeps the counter consistent even if rows get
        # cleaned out separately later — denormalization tracking truth.
        if url_result.rowcount and url_result.rowcount > 0:
            new_count = await s.scalar(
                select(func.count(LeadSourceUrl.lead_id)).where(
                    LeadSourceUrl.lead_id == lead_id
                )
            )
            await s.execute(
                update(Lead).where(Lead.id == lead_id).values(source_count=new_count or 1)
            )
        await s.commit()
    return existed is None


async def _set_status(sr_id: int, status: str) -> None:
    async with SessionLocal() as s:
        await s.execute(update(SearchResult).where(SearchResult.id == sr_id).values(status=status))
        await s.commit()


async def extract_for_campaign(campaign_id: int) -> dict[str, int]:
    """Stage 5: scan every extractable search_result, pull invite_ids, UPSERT leads.

    Picks up:
      - fetch_strategy='snippet_hit' AND status='new'  (no fetch was needed)
      - fetch_strategy in ('reddit','web') AND status='fetched'

    Idempotent: rows already 'extracted' are skipped.
    """
    counts = {"new_leads": 0, "updated_leads": 0, "extracted_rows": 0, "no_invite": 0}

    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(
                    SearchResult.id,
                    SearchResult.url,
                    SearchResult.title,
                    SearchResult.snippet,
                    SearchResult.fetch_strategy,
                    SearchResult.status,
                )
                .join(Query, SearchResult.query_id == Query.id)
                .where(
                    Query.campaign_id == campaign_id,
                    or_(
                        (SearchResult.fetch_strategy == "snippet_hit")
                        & (SearchResult.status == "new"),
                        (SearchResult.fetch_strategy.in_(["reddit", "web"]))
                        & (SearchResult.status == "fetched"),
                    ),
                )
            )
        ).all()

    if not rows:
        logger.info("stage 5: no extractable rows for campaign %s", campaign_id)
        return counts

    for sr_id, url, title, snippet, strategy, status in rows:
        if strategy == "snippet_hit":
            text = "\n".join(filter(None, [url, title, snippet]))
        else:
            async with SessionLocal() as s:
                cached = await s.get(UrlCache, url)
            if cached is None:
                logger.warning("stage 5: cache miss for fetched sr=%d url=%s", sr_id, url)
                await _set_status(sr_id, "extracted")
                continue
            text = cached.markdown

        invites = extract_invites(text)
        if not invites:
            counts["no_invite"] += 1
            await _set_status(sr_id, "extracted")
            continue

        for invite_id, context in invites:
            is_new = await _upsert_lead(invite_id, url, title, context, campaign_id)
            counts["new_leads" if is_new else "updated_leads"] += 1

        counts["extracted_rows"] += 1
        await _set_status(sr_id, "extracted")

    logger.info("stage 5 done for campaign %s: %s", campaign_id, counts)
    return counts
