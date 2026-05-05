"""Generic stage-5 helpers used by every platform's extractor.

Each platform's `extract.py` only needs to define:
  - an `_INVITE_RE` for its invite-link shape
  - a small `extract_invites(text)` function that returns
    [(invite_id, ±200char context), ...]

…and then call `run_extract_for_campaign(campaign_id, extract_invites)` to
get all the boilerplate (search_result iteration, cache lookup, lead upsert,
source-URL junction maintenance, `source_count` recompute).

Lives in `shared/` because none of this logic is platform-specific. The
only platform input is the regex closure passed in — same Lead table,
same junction, same status transitions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db.models import Lead, LeadSourceUrl, Query, SearchResult, UrlCache
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)


ExtractFn = Callable[[str | None], list[tuple[str, str]]]


async def upsert_lead(
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

        lead_id = await s.scalar(
            select(Lead.id).where(
                Lead.campaign_id == campaign_id,
                Lead.invite_id == invite_id,
            )
        )

        url_stmt = pg_insert(LeadSourceUrl).values(lead_id=lead_id, url=source_url)
        url_stmt = url_stmt.on_conflict_do_nothing(index_elements=["lead_id", "url"])
        url_result = await s.execute(url_stmt)
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


async def mark_search_result_extracted(sr_id: int) -> None:
    async with SessionLocal() as s:
        await s.execute(
            update(SearchResult).where(SearchResult.id == sr_id).values(status="extracted")
        )
        await s.commit()


async def run_extract_for_campaign(
    campaign_id: int,
    extract_invites: ExtractFn,
) -> dict[str, int]:
    """Stage 5: scan every extractable search_result, pull invite_ids using the
    platform's `extract_invites` callable, UPSERT leads.

    Picks up:
      - fetch_strategy='snippet_hit' AND status='new'  (no fetch was needed)
      - fetch_strategy in ('reddit','web') AND status='fetched'

    Idempotent: rows already 'extracted' are skipped.

    The `extract_invites` callable is the platform's contribution — it
    receives the raw text and returns [(invite_id, context), ...]. Anything
    that returns an empty list on a platform-mismatched URL (e.g. running
    the WA extractor on a Discord-only thread) gets recorded as no_invite,
    which is the desired behavior — cross-platform leakage doesn't pollute
    leads.
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
        logger.info("extract: no extractable rows for campaign %s", campaign_id)
        return counts

    for sr_id, url, title, snippet, strategy, _status in rows:
        if strategy == "snippet_hit":
            text = "\n".join(filter(None, [url, title, snippet]))
        else:
            async with SessionLocal() as s:
                cached = await s.get(UrlCache, url)
            if cached is None:
                logger.warning("extract: cache miss for fetched sr=%d url=%s", sr_id, url)
                await mark_search_result_extracted(sr_id)
                continue
            text = cached.markdown

        invites = extract_invites(text)
        if not invites:
            counts["no_invite"] += 1
            await mark_search_result_extracted(sr_id)
            continue

        for invite_id, context in invites:
            is_new = await upsert_lead(invite_id, url, title, context, campaign_id)
            counts["new_leads" if is_new else "updated_leads"] += 1

        counts["extracted_rows"] += 1
        await mark_search_result_extracted(sr_id)

    logger.info("extract done for campaign %s: %s", campaign_id, counts)
    return counts
