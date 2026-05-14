"""Subreddit-direct cold-start mining — seed a campaign with invite-bearing
Reddit threads BEFORE any leads exist.

How this differs from `subreddit_discovery.py`:
  - `subreddit_discovery` is SNOWBALL: re-mines subs that already produced
    a lead in this campaign. Needs ≥1 lead to fire.
  - `subreddit_seed` (this module) is COLD-START: picks subs from a curated
    industry → subreddits map and searches them for the platform's invite
    URL substring. Fires on the very first run, when no leads exist yet.

The two are complementary. Run cold-start once per campaign as part of the
orchestrator; let snowball run as leads accumulate.

Why the search query is the platform anchor (e.g. `chat.whatsapp.com`) and
NOT the industry term:
  - The SUBREDDIT itself is the industry filter — if we picked r/digital_marketing,
    any invite there is already in-domain.
  - Reddit's in-sub search is a substring match across title+selftext+url, so
    searching the anchor returns posts that literally contain the URL — the
    highest-precision query possible. Industry-keyword search adds recall on
    posts that ASK for invites but don't show one in the OP; we lean toward
    precision here because the snowball stage handles the "ask" posts later.

Cost: zero Serper / Firecrawl credits. One `subreddit.search` call per sub;
script-app quota is 600 req/10min so a ~15-sub pass uses ~3% of the budget.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from src.clients.reddit_client import (
    RedditUnreachable,
    reddit_session,
    search_subreddit,
)
from src.db.models import Campaign, Query, SearchResult
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)


# Per-platform search anchor. Reddit's in-sub search is a token/substring
# match — searching the anchor returns posts that literally contain the
# invite URL. One anchor per platform; we deliberately don't OR-combine
# variants (e.g. discord.com/invite) because the .gg form dominates real
# usage and adding OR-clauses inflates the result set with low-precision
# matches on the rare second form.
PLATFORM_ANCHORS: dict[str, str] = {
    "whatsapp": "chat.whatsapp.com",
    "discord": "discord.gg",
    "slack": "join.slack.com",
}


# Per-industry curated subreddit lists. The first time a campaign runs, we
# look up the industry name here (case-insensitive, first-match) and search
# every sub for the platform anchor. Entries are sub display names without
# the `r/` prefix.
#
# Curation principles:
#   - High-traffic subs where invites are routinely shared in posts/comments
#   - Avoid niche subs <5k subscribers (signal is too sparse)
#   - Skip generic/meta subs (e.g. r/AskReddit) — too broad, low density
#
# When an industry isn't matched, we fall back to DEFAULT_SUBS — a small set
# of broad business communities where most B2B invites surface.
INDUSTRY_TO_SUBREDDITS: dict[str, list[str]] = {
    "marketing": [
        "marketing", "digital_marketing", "PPC", "SEO", "socialmedia",
        "content_marketing", "EmailMarketing", "advertising",
        "InboundMarketing", "Affiliatemarketing", "B2B_Marketing",
        "smallbusiness", "Entrepreneur", "SaaS", "growmybusiness",
    ],
    "saas": [
        "SaaS", "Entrepreneur", "startups", "indiehackers", "smallbusiness",
        "EntrepreneurRideAlong", "growmybusiness", "B2B_Marketing",
        "ProductManagement", "ycombinator",
    ],
    "ecommerce": [
        "ecommerce", "shopify", "FulfillmentByAmazon", "dropship",
        "AmazonSeller", "BehindTheClosetDoor", "Entrepreneur",
        "smallbusiness",
    ],
    "agency": [
        "agency", "marketing", "digital_marketing", "freelance",
        "smallbusiness", "Entrepreneur", "EntrepreneurRideAlong",
        "PPC", "SEO",
    ],
    "ai": [
        "artificial", "MachineLearning", "OpenAI", "LocalLLaMA",
        "ChatGPT", "singularity", "AI_Agents", "LangChain",
        "startups", "Entrepreneur",
    ],
    "real estate": [
        "RealEstate", "realtors", "RealEstateInvesting", "realestateinvesting",
        "FirstTimeHomeBuyer", "smallbusiness",
    ],
    "crypto": [
        "CryptoCurrency", "CryptoMarkets", "ethfinance", "ethereum",
        "Bitcoin", "defi", "solana",
    ],
    "fitness": [
        "fitness", "loseit", "personaltraining", "bodybuilding",
        "Fitness", "GYM", "smallbusiness",
    ],
    "freelance": [
        "freelance", "freelanceWriters", "WorkOnline", "Entrepreneur",
        "smallbusiness", "digitalnomad",
    ],
    "design": [
        "graphic_design", "web_design", "userexperience", "UI_Design",
        "Design", "Entrepreneur", "freelance",
    ],
}


# Fallback subs when the campaign's industry doesn't match any curated entry.
# These are broad B2B/founder communities — lower density per sub than a
# tightly-matched industry list, but reasonable coverage when we have no
# better signal. Curated to avoid mega-generic subs (e.g. r/AskReddit) that
# would mostly return noise.
DEFAULT_SUBS: list[str] = [
    "Entrepreneur", "smallbusiness", "startups", "EntrepreneurRideAlong",
    "indiehackers", "SaaS", "growmybusiness", "freelance",
]


# Per-subreddit cap on submissions pulled from search. Beyond ~50 the yield
# tails off (old invites are dead — Slack auto-expires in 30d, WA churns too)
# and stage 4a comment expansion gets expensive. 50 hits the sweet spot.
SUB_SEARCH_LIMIT = 50

# Per-pass cap on how many subs we mine in one shot. With ~15 industries and
# 8-15 subs each, this is rarely the binding constraint, but it bounds the
# Reddit API spend per campaign start.
MAX_SUBS_PER_PASS = 20


def _resolve_subs_for_campaign(campaign: Campaign) -> list[str]:
    """Pick the subreddit list for this campaign based on its first industry.

    Match is case-insensitive and first-token-substring: a campaign with
    industry "Marketing agency" matches the "marketing" key (any industry
    that starts with or contains a curated key picks up that list). If
    nothing matches, DEFAULT_SUBS is returned.
    """
    industries = [s for s in (campaign.industries or []) if s and s.strip()]
    if not industries:
        return list(DEFAULT_SUBS)

    primary = industries[0].strip().lower()
    # Exact match first (cheap)
    if primary in INDUSTRY_TO_SUBREDDITS:
        return list(INDUSTRY_TO_SUBREDDITS[primary])
    # Substring match — `"marketing agency"` should hit the `"marketing"` key.
    for key, subs in INDUSTRY_TO_SUBREDDITS.items():
        if key in primary or primary in key:
            return list(subs)
    return list(DEFAULT_SUBS)


def _resolve_platform_anchor(platform: str | None) -> str | None:
    """Return the search anchor for the campaign's platform, or None if the
    platform isn't supported. Callers should treat None as "skip seeding"."""
    if not platform:
        return None
    return PLATFORM_ANCHORS.get(platform.lower())


async def seed_subreddits_for_campaign(campaign_id: int) -> dict[str, int]:
    """Cold-start subreddit seeding for a campaign. Returns counts dict:
      - subs_searched: how many subs we actually queried
      - new_search_results: count of NEW SearchResult rows inserted
      - skipped_existing: count of submissions whose URLs were already known
      - errors: subs that errored out (private, banned, network blip)

    Idempotent: skips URLs already present in the campaign's search_results.
    Re-running adds only newly-indexed submissions.

    Returns early (all zeros) when:
      - Campaign doesn't exist
      - Campaign has no platform we support
      - Reddit API is unreachable (worker logs a warning; doesn't crash the
        pipeline — caller should continue)
    """
    counts = {
        "subs_searched": 0,
        "new_search_results": 0,
        "skipped_existing": 0,
        "errors": 0,
    }

    async with SessionLocal() as s:
        campaign = await s.get(Campaign, campaign_id)
        if campaign is None:
            return counts

        anchor = _resolve_platform_anchor(campaign.platform)
        if anchor is None:
            logger.info(
                "subreddit-seed: campaign %s has unsupported platform %r; skipping",
                campaign_id, campaign.platform,
            )
            return counts

        subs = _resolve_subs_for_campaign(campaign)[:MAX_SUBS_PER_PASS]
        if not subs:
            return counts

        # Single anchor Query so all seeded results group cleanly in /activity
        # and stage 4a treats them as a coherent batch.
        anchor_query = Query(
            campaign_id=campaign_id,
            query_text=f"[subreddit-seed] {anchor}",
            source_platform="reddit",
            status="searched",
        )
        s.add(anchor_query)
        await s.flush()
        anchor_query_id = anchor_query.id

        # Cache existing URLs to avoid double-inserts. Includes URLs from prior
        # Serper searches AND prior subreddit-seed/discovery runs.
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
            for sub_name in subs:
                try:
                    submissions = await search_subreddit(
                        reddit, sub_name, anchor, limit=SUB_SEARCH_LIMIT
                    )
                except RedditUnreachable as e:
                    # API died mid-pass — stop here; next run resumes cleanly
                    # because nothing is marked failed.
                    logger.warning(
                        "subreddit-seed: Reddit unreachable mid-pass: %s", e
                    )
                    break

                counts["subs_searched"] += 1
                if not submissions:
                    continue

                async with SessionLocal() as s:
                    rows: list[SearchResult] = []
                    for sub in submissions:
                        permalink = getattr(sub, "permalink", None)
                        if not permalink:
                            continue
                        url = f"https://www.reddit.com{permalink}".rstrip("/")
                        if url in existing_urls:
                            counts["skipped_existing"] += 1
                            continue
                        existing_urls.add(url)
                        rows.append(
                            SearchResult(
                                query_id=anchor_query_id,
                                url=url,
                                title=getattr(sub, "title", None),
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
        # Auth itself failed — we never got into the loop. Mark every
        # planned sub as an error for telemetry, but don't crash.
        logger.warning(
            "subreddit-seed: could not authenticate with Reddit: %s", e
        )
        counts["errors"] = len(subs)

    logger.info(
        "subreddit-seed: campaign %s (%s/%s): %s",
        campaign_id, campaign.platform, anchor, counts,
    )
    return counts
