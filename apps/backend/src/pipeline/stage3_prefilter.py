import logging
import re
from typing import Literal
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Query, SearchResult

logger = logging.getLogger(__name__)

FetchStrategy = Literal["snippet_hit", "reddit", "web", "skip"]

# Captures the invite_id portion. Whatsapp invites are alnum + '_' + '-'.
WHATSAPP_INVITE_RE = re.compile(r"chat\.whatsapp\.com/[A-Za-z0-9_-]+", re.IGNORECASE)

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


def classify(url: str, title: str | None = None, snippet: str | None = None) -> FetchStrategy:
    """Pure classifier — no IO. Order matters:
      1) snippet_hit if invite link is already visible (URL/title/snippet)
      2) reddit if host is a reddit subdomain
      3) skip for known-blocked hosts or file extensions
      4) web otherwise
    """
    haystacks = (url, title or "", snippet or "")
    if any(WHATSAPP_INVITE_RE.search(h) for h in haystacks):
        return "snippet_hit"

    host = _hostname(url)

    if _is_reddit(host):
        return "reddit"

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
