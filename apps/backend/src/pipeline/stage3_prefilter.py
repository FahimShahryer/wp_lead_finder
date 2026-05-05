import logging
import re
from typing import Literal
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Query, SearchResult

logger = logging.getLogger(__name__)

FetchStrategy = Literal["snippet_hit", "reddit", "web", "skip"]

# Detects whether ANY whatsapp invite link is present in a string. Whatsapp
# invite_ids are alnum + '_' + '-'; bound to {6,30} chars to reject trivial
# false positives (matches stage 5's stricter extraction regex).
WHATSAPP_INVITE_RE = re.compile(
    r"chat\.whatsapp\.com/[A-Za-z0-9_-]{6,30}(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)

# Domains we don't try to fetch — either anti-scraping (LinkedIn, Twitter),
# media-only (YouTube), or low-yield for invite links.
SKIP_HOST_SUFFIXES: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "facebook.com",
    "fbcdn.net",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "pinterest.com",
    "imgur.com",
    "i.redd.it",
    "i.imgur.com",
    "pbs.twimg.com",
    "media.giphy.com",
)

# Curated blocklist of WhatsApp invite-link aggregator / "directory" sites.
# These pages SEO-target the exact `"chat.whatsapp.com" "<industry>"` query
# shape we generate, then return 50-500 mostly-dead, mixed-category invites
# per page — auto-scraped from elsewhere, not posted by real community owners.
# Skipping them at the prefilter stage:
#   - Drops noise leads BEFORE they enter the lead lifecycle
#   - Saves Firecrawl credits we'd otherwise spend fetching giant listicles
#   - Saves WA-enrichment requests on confirmed-dead invites
#   - Prevents the source-recurrence bonus from being inflated by the same
#     dead invite appearing on 5 different directories
#
# Conservative curated list — pattern-based heuristics risk false positives
# (e.g. legitimate "whatsapp-business.com" Meta property). Add new ones as
# they surface in real campaigns. Subdomain-aware (matches `foo.bar.com`).
WA_DIRECTORY_DOMAINS: tuple[str, ...] = (
    "newwhatsgroups.com",
    "whatsgroupjoinlinks.com",
    "wagroupslink.com",
    "whatgroups.com",
    "whatsgroulinks.com",
    "whtspgrouplink.com",
    "thewhatsgrouplink.com",
    "wappgrouplinks.com",
    "joinchatgroups.com",
    "whtsgroupslinkspk.com",
    "wa-filter.com",
    "wa-contact-extractor.com",
)

SKIP_FILE_EXTS: tuple[str, ...] = (
    ".pdf",
    ".doc", ".docx",
    ".ppt", ".pptx",
    ".xls", ".xlsx",
    ".zip", ".rar", ".7z",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".m4a",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
)


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_reddit(host: str) -> bool:
    return host == "reddit.com" or host.endswith(".reddit.com")


def _is_skip_host(host: str) -> bool:
    if not host:
        return False
    for suffix in SKIP_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _is_skip_extension(url: str) -> bool:
    try:
        path = (urlparse(url).path or "").lower()
    except Exception:
        return False
    return any(path.endswith(ext) for ext in SKIP_FILE_EXTS)


def _is_wa_directory(host: str) -> bool:
    """True if the host matches a known WhatsApp invite aggregator. Subdomain-
    aware: `m.whtspgrouplink.com` and `whtspgrouplink.com` both match."""
    if not host:
        return False
    for suffix in WA_DIRECTORY_DOMAINS:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def classify(url: str, title: str | None = None, snippet: str | None = None) -> FetchStrategy:
    """Pure classifier — no IO. Order matters:
      1) reddit if host is a reddit subdomain — even if an invite peeked into
         the snippet, the thread + comments often hold 10–30× more invites; we
         never short-circuit Reddit to snippet_hit. asyncpraw fetches are free,
         so the only cost is one extra API call that almost always pays for itself.
      2) skip if the host is a known WhatsApp directory aggregator. Comes
         BEFORE the snippet_hit check on purpose — these sites usually leak
         the invite into the snippet, but the lead would be noise (auto-
         scraped, mostly dead, mixed-category). Better to drop entirely than
         pollute the lead lifecycle.
      3) snippet_hit if the invite link is already visible (URL/title/snippet).
         Covers chat.whatsapp.com URLs themselves, hard-firewall sites where we
         can't fetch (facebook/linkedin/x), and small web pages where saving the
         Firecrawl credit is worth more than re-extracting from the full page.
      4) skip for known-blocked hosts or file extensions.
      5) web otherwise → Firecrawl.
    """
    host = _hostname(url)

    if _is_reddit(host):
        return "reddit"

    # Directory aggregators — block BEFORE snippet_hit so we don't grab the
    # noise invites these sites leak into Google's snippets.
    if _is_wa_directory(host):
        return "skip"

    haystacks = (url, title or "", snippet or "")
    if any(WHATSAPP_INVITE_RE.search(h) for h in haystacks):
        return "snippet_hit"

    if _is_skip_host(host) or _is_skip_extension(url):
        return "skip"

    return "web"


async def prefilter_search_results(
    session: AsyncSession, campaign_id: int
) -> dict[str, int]:
    """Stage 3: tag every untagged search_result with a fetch_strategy.

    Idempotent: only processes rows where fetch_strategy IS NULL.
    Returns counts per strategy for the rows that were *just* tagged.
    """
    rows = (
        await session.execute(
            select(SearchResult)
            .join(Query, SearchResult.query_id == Query.id)
            .where(
                Query.campaign_id == campaign_id,
                SearchResult.fetch_strategy.is_(None),
            )
        )
    ).scalars().all()

    counts: dict[str, int] = {"snippet_hit": 0, "reddit": 0, "web": 0, "skip": 0}
    for sr in rows:
        strategy = classify(sr.url, sr.title, sr.snippet)
        sr.fetch_strategy = strategy
        counts[strategy] += 1

    await session.commit()
    logger.info("stage 3 done for campaign %s: %s", campaign_id, counts)
    return counts
