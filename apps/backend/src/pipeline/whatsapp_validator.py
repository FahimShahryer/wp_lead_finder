import asyncio
import html
import json
import logging
import random
import re
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis

from src.core.config import settings

logger = logging.getLogger(__name__)

# WhatsApp invite landing pages render the group name into og:title and the
# group description into og:description ONLY when the invite is still live.
# Revoked/expired invites return the generic placeholder page with no og:title.
OG_TITLE_RE = re.compile(r'<meta property="og:title" content="([^"]*)"')
OG_DESC_RE = re.compile(r'<meta property="og:description" content="([^"]*)"')

# Markers indicating Meta's edge served a CAPTCHA / challenge / block page
# instead of the real invite page. These are ONLY consulted on small-body
# responses (real invite pages are 150KB+; block/interstitial pages are <5KB),
# so common words like "blocked" appearing in a real group description don't
# cause false positives.
BLOCK_MARKERS = (
    "captcha",
    "challenge-platform",
    "just a moment",
    "cf-error",
    "access denied",
    "automated requests",
    "unusual traffic",
)
# Real invite pages — both valid and invalid invites — render to >150KB. If
# we see substantially less than that on a 200 response, something else is
# going on (an interstitial / soft-block / network truncation).
MIN_REAL_PAGE_BYTES = 5000

CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days — group names rarely change
CACHE_PREFIX = "wa:invite:v2:"
CONCURRENCY = 3  # human-paced; safer for shared egress IP
TIMEOUT_SECONDS = 15.0
JITTER_MIN_SECONDS = 0.10
JITTER_MAX_SECONDS = 0.50
DEFAULT_REQUEST_BUDGET = 500  # circuit breaker per run
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Most WhatsApp invite pages return a localized boilerplate as og:description
# (e.g. "WhatsApp Group Invite", "Convite para grupo do WhatsApp", "Invitation
# au groupe WhatsApp", ...). It carries no information about THIS group, so we
# strip it. Real custom descriptions are typically longer than this threshold;
# anything short that mentions "whatsapp" is almost certainly a locale default.
GENERIC_DESC_MAX_LEN = 50


class WhatsAppBlocked(Exception):
    """Meta's WAF returned a CAPTCHA / challenge / 4xx block. Caller should
    halt the run — continuing would mark legitimate groups as invalid."""


class WhatsAppBudgetExceeded(Exception):
    """Per-run request budget hit. Caller should stop and either bump the
    budget or split the run into smaller chunks."""


@dataclass
class InviteInfo:
    valid: bool
    group_name: str | None
    group_description: str | None


def _cache_key(invite_id: str) -> str:
    return f"{CACHE_PREFIX}{invite_id}"


def _looks_blocked(status: int, body: str) -> bool:
    if status in (403, 429, 503):
        return True
    # We only call something blocked on a 200 response if it's TINY (real
    # invite pages, valid or invalid, render to >150KB) AND looks like an
    # interstitial. Common words like "blocked" appearing inside a real
    # 200KB invite page must NOT trigger this — that would silently
    # falsify good leads.
    if status == 200 and len(body) < MIN_REAL_PAGE_BYTES:
        lo = body.lower()
        if any(m in lo for m in BLOCK_MARKERS) or not OG_TITLE_RE.search(body):
            return True
    return False


def _parse_invite(html_text: str) -> InviteInfo:
    title_m = OG_TITLE_RE.search(html_text)
    desc_m = OG_DESC_RE.search(html_text)
    name = html.unescape(title_m.group(1)).strip() if title_m else ""
    desc_raw = html.unescape(desc_m.group(1)).strip() if desc_m else ""
    if not name or name.lower() == "whatsapp":
        return InviteInfo(valid=False, group_name=None, group_description=None)
    desc: str | None = None
    if desc_raw:
        is_generic = (
            len(desc_raw) <= GENERIC_DESC_MAX_LEN
            and "whatsapp" in desc_raw.lower()
        )
        if not is_generic:
            desc = desc_raw
    return InviteInfo(valid=True, group_name=name, group_description=desc)


async def _fetch_one(
    client: httpx.AsyncClient, invite_id: str
) -> InviteInfo:
    """Hit WhatsApp once for a single invite_id. Raises WhatsAppBlocked on a
    challenge/4xx page; returns InviteInfo otherwise. Caller is responsible
    for jitter — applied before this is called."""
    r = await client.get(f"https://chat.whatsapp.com/{invite_id}")
    if _looks_blocked(r.status_code, r.text):
        raise WhatsAppBlocked(
            f"WhatsApp returned a block/challenge for {invite_id} "
            f"(status={r.status_code}, body_len={len(r.text)})"
        )
    return _parse_invite(r.text)


def _serialize(info: InviteInfo) -> str:
    return json.dumps(
        {
            "valid": info.valid,
            "group_name": info.group_name,
            "group_description": info.group_description,
        }
    )


def _deserialize(raw: str) -> InviteInfo | None:
    try:
        d = json.loads(raw)
        return InviteInfo(
            valid=bool(d["valid"]),
            group_name=d.get("group_name"),
            group_description=d.get("group_description"),
        )
    except Exception:
        return None


async def fetch_invites(
    invite_ids: list[str],
    redis: Redis,
    *,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
    concurrency: int = CONCURRENCY,
    use_cache: bool = True,
) -> dict[str, InviteInfo]:
    """Resolve name + description + validity for each invite_id with full
    hardening: 7d Redis cache, jittered low-concurrency network fetches,
    block-page detection (raises WhatsAppBlocked), and a per-run request
    budget (raises WhatsAppBudgetExceeded).

    Transient network errors are NOT cached and not present in the result —
    callers should treat absent keys as "unknown, retry next time".
    """
    if not invite_ids:
        return {}

    out: dict[str, InviteInfo] = {}
    misses: list[str] = []

    if use_cache:
        cached = await redis.mget(*[_cache_key(i) for i in invite_ids])
        for invite_id, raw in zip(invite_ids, cached):
            info = _deserialize(raw) if raw is not None else None
            if info is None:
                misses.append(invite_id)
            else:
                out[invite_id] = info
    else:
        misses = list(invite_ids)

    if not misses:
        return out

    if len(misses) > request_budget:
        raise WhatsAppBudgetExceeded(
            f"would need {len(misses)} fetches; budget is {request_budget}. "
            "Bump the budget or run a smaller batch."
        )

    sem = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": USER_AGENT}
    proxy = settings.whatsapp_proxy or None
    if proxy:
        logger.info("whatsapp_validator: routing through proxy")

    fetched = 0

    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
        headers=headers,
        proxy=proxy,
    ) as client:

        async def run(invite_id: str) -> tuple[str, InviteInfo | None]:
            nonlocal fetched
            async with sem:
                # Random jitter before each fetch — feels human-paced and
                # avoids burst signatures even at low concurrency.
                await asyncio.sleep(random.uniform(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS))
                fetched += 1
                try:
                    info = await _fetch_one(client, invite_id)
                    return invite_id, info
                except WhatsAppBlocked:
                    raise
                except Exception as e:
                    logger.warning("invite fetch failed for %s: %s", invite_id, e)
                    return invite_id, None

        try:
            results = await asyncio.gather(*[run(i) for i in misses])
        except WhatsAppBlocked as e:
            logger.error("whatsapp_validator: aborting batch — %s", e)
            raise

    pipe = redis.pipeline()
    for invite_id, info in results:
        if info is None:
            # Transient — don't cache, leave caller to retry.
            continue
        out[invite_id] = info
        pipe.setex(_cache_key(invite_id), CACHE_TTL_SECONDS, _serialize(info))
    await pipe.execute()
    logger.info(
        "whatsapp_validator: %d cached, %d fetched, %d resolved",
        len(out) - len([1 for r in results if r[1] is not None]),
        fetched,
        len(out),
    )
    return out


# ---- Backwards-compatible thin wrapper used by filter/export endpoints ----

@dataclass
class ValidityInfo:
    valid: bool
    group_name: str | None


async def validate_invites(
    invite_ids: list[str], redis: Redis
) -> dict[str, ValidityInfo]:
    """Legacy compatibility shim: returns just (valid, group_name) pairs.
    Reads from cache only; never triggers fetches. Run the enrichment endpoint
    first to populate the cache + DB."""
    if not invite_ids:
        return {}
    cached = await redis.mget(*[_cache_key(i) for i in invite_ids])
    out: dict[str, ValidityInfo] = {}
    for invite_id, raw in zip(invite_ids, cached):
        if raw is None:
            continue
        info = _deserialize(raw)
        if info is None:
            continue
        out[invite_id] = ValidityInfo(valid=info.valid, group_name=info.group_name)
    return out
