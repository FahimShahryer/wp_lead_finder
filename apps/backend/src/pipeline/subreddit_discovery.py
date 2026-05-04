"""Subreddit-direct discovery — mine productive subs via asyncpraw search.

Once a campaign has yielded ≥1 lead from a Reddit URL, the subreddit that
hosted it is now a known-good source for this ICP. Reddit's own search index
typically holds many more invite-bearing threads in that sub than Google
ever surfaces, especially for older posts that lack SEO-optimized titles.

This module:
  1. Identifies productive subreddits from the campaign's existing leads
     (any subreddit that produced ≥ MIN_LEADS_FOR_PRODUCTIVE invite).
  2. Runs `subreddit.search("<industry> whatsapp")` on each, capped at
     SUB_SEARCH_LIMIT submissions per sub.
  3. Inserts the resulting submission URLs as new SearchResult rows tagged
     `fetch_strategy='reddit'`, `status='new'`. The next stage 4a run picks
     them up and walks the comment trees.

Designed to be re-runnable: dedupes URLs against existing search_results so
calling it multiple times in the same campaign doesn't add duplicates.

Cost: zero Serper or Firecrawl credits. Reddit script-app quota is 600
req/10min; one search call per sub is well under that.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from sqlalchemy import select

from src.clients.reddit_client import (
    RedditUnreachable,
    reddit_session,
    search_subreddit,
)
from src.db.models import Campaign, Lead, Query, SearchResult
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)


# Minimum leads from a sub before we consider it "productive enough" to
# warrant a direct mine. With 1, the very first false positive on a generic
# sub like r/whatsapp would trigger a full search; 1 is still our default
# because most leads are extracted from snippet hits and the cost of mining
# a "wrong" sub is low (one asyncpraw search call).
MIN_LEADS_FOR_PRODUCTIVE = 1

# Per-subreddit cap on submissions returned by the search. Beyond ~25 the
# yield drops fast (older threads have dead invites) and we'd risk pushing
# stage 4a comment-tree expansion past its rate limits.
SUB_SEARCH_LIMIT = 25

# Per-discovery-pass cap on subreddits to mine. If a campaign somehow
# produced leads across 200 different subs, we don't want to fan out into
# 200 search calls — pick the top-N by lead count.
MAX_SUBS_PER_PASS = 20


_SUBREDDIT_URL_RE = re.compile(
    r"^https?://(?:www\.|old\.|np\.|m\.)?reddit\.com/r/([A-Za-z0-9_]+)",
    re.IGNORECASE,
)


def _extract_subreddit_from_url(url: str) -> str | None:
    """Pull the sub name from a Reddit URL, lowercase. None if not a Reddit URL."""
    if not url:
        return None
    m = _SUBREDDIT_URL_RE.match(url)
    return m.group(1).lower() if m else None


async def _productive_subs(campaign_id: int) -> list[tuple[str, int]]:
    """Return [(sub_name, lead_count), ...] sorted by lead_count desc, for
    every sub that has produced >= MIN_LEADS_FOR_PRODUCTIVE leads in this
    campaign."""
    async with SessionLocal() as s:
        urls = list(
            (
                await s.execute(
                    select(Lead.source_url).where(
                        Lead.campaign_id == campaign_id,
                        Lead.source_url.isnot(None),
                    )
                )
            ).scalars()
        )
    counts: dict[str, int] = {}
    for url in urls:
        sub = _extract_subreddit_from_url(url)
        if not sub:
            continue
        counts[sub] = counts.get(sub, 0) + 1
    productive = [
        (sub, n) for sub, n in counts.items() if n >= MIN_LEADS_FOR_PRODUCTIVE
    ]
    productive.sort(key=lambda x: -x[1])
    return productive[:MAX_SUBS_PER_PASS]


def _build_search_query(campaign: Campaign) -> str:
    """Compose the within-sub search query. We keep it short — sub-search is
    a substring/boolean match, not Google, so long phrases hurt recall.
    Format: `<first industry term> whatsapp`. With no industry, fall back
    to `whatsapp invite` so the search still has a meaningful anchor word."""
    industries = [s.strip() for s in (campaign.industries or []) if s and s.strip()]
    if not industries:
        return "whatsapp invite"
    return f"{industries[0]} whatsapp"


async def discover_via_productive_subreddits(campaign_id: int) -> dict[str, int]:
    """Top-level entry. Returns counts dict:
      - subs_mined: how many subreddits we actually searched
      - new_search_results: count of NEW SearchResult rows inserted
      - skipped_existing: count of submissions whose URLs were already known
      - errors: count of subs we couldn't search (private/banned/network)

    Inserts new SearchResult rows under a single anchor Query so they're
    grouped for traceability. The orchestrator's stage 4a will pick them up
    on the next run.
    """
    counts = {
        "subs_mined": 0,
        "new_search_results": 0,
        "skipped_existing": 0,
        "errors": 0,
    }

    productive = await _productive_subs(campaign_id)
    if not productive:
        logger.info("subreddit-discovery: no productive subs for campaign %s", campaign_id)
        return counts

    async with SessionLocal() as s:
        campaign = await s.get(Campaign, campaign_id)
        if campaign is None:
            return counts
        search_query = _build_search_query(campaign)

        # All discovery results land under a single anchor Query so they're
        # easy to filter in /activity and so stage 4a treats them as a batch.
        anchor = Query(
            campaign_id=campaign_id,
            query_text=f"[subreddit-direct] {search_query}",
            source_platform="reddit",
            status="searched",
        )
        s.add(anchor)
        await s.flush()
        anchor_id = anchor.id

        # Cache existing URLs in this campaign so we don't double-insert.
        existing_urls: set[str] = set(
            (
                await s.execute(
                    select(SearchResult.url)
                    .join(Query, SearchResult.query_id == Query.id)
                    .where(Query.campaign_id == campaign_id)
                )
            ).scalars()
        )
        await s.commit()

    try:
        async with reddit_session() as reddit:
            for sub_name, _lead_count in productive:
                try:
                    submissions = await search_subreddit(
                        reddit, sub_name, search_query, limit=SUB_SEARCH_LIMIT
                    )
                except RedditUnreachable as e:
                    # API itself is down — stop the pass; the next campaign
                    # run can retry. Don't mark anything as failed.
                    logger.warning(
                        "subreddit-discovery: Reddit unreachable mid-pass: %s", e
                    )
                    break

                counts["subs_mined"] += 1
                if not submissions:
                    continue

                async with SessionLocal() as s:
                    rows: list[SearchResult] = []
                    for submission in submissions:
                        permalink = getattr(submission, "permalink", None)
                        if not permalink:
                            continue
                        url = f"https://www.reddit.com{permalink}"
                        # Normalize trailing slash so the existing-URL check
                        # is consistent regardless of how Google formatted it.
                        url = url.rstrip("/")
                        if url in existing_urls:
                            counts["skipped_existing"] += 1
                            continue
                        existing_urls.add(url)
                        rows.append(
                            SearchResult(
                                query_id=anchor_id,
                                url=url,
                                title=getattr(submission, "title", None),
                                snippet=None,
                                position=None,
                                status="new",
                                fetch_strategy="reddit",
                            )
                        )
                    if rows:
                        s.add_all(rows)
                        await s.commit()
                        counts["new_search_results"] += len(rows)
    except RedditUnreachable as e:
        # Auth itself failed — we never got into the loop.
        logger.warning(
            "subreddit-discovery: could not authenticate with Reddit: %s", e
        )
        counts["errors"] = len(productive)

    logger.info(
        "subreddit-discovery: campaign %s: %s (productive subs: %d)",
        campaign_id, counts, len(productive),
    )
    return counts
