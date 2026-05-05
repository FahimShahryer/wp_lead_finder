"""Discord invite validator — hits Discord's public API to verify each
invite code resolves to a live server.

Why Discord is dramatically simpler than WhatsApp here:
  - Public API endpoint: GET https://discord.com/api/v10/invites/{code}
  - No auth required (it's a public resource)
  - No CAPTCHA / challenge machinery
  - Returns clean JSON with server name, description, member count
  - Allowed rate is generous (~50 req/sec on this endpoint)

So no Redis-backed rate limiter, no per-IP cooldown logic — just a bounded
async fan-out with a small concurrency cap and per-attempt timeout.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Discord's published rate is ~50 req/sec on this endpoint without auth.
# We sit comfortably under that with Semaphore(20).
DISCORD_CONCURRENCY = 20

# Per-call timeout. The Discord API is fast; 10s is generous.
REQUEST_TIMEOUT = 10.0

# Default budget cap on a single enrich pass — same shape as WA's so the
# enricher orchestrator can use the same interface.
DEFAULT_REQUEST_BUDGET = 200

INVITE_API_URL = "https://discord.com/api/v10/invites/{code}?with_counts=true"


class DiscordBudgetExceeded(Exception):
    """Raised when caller asks for more invites than `request_budget`."""


@dataclass
class InviteInfo:
    """Result of validating one Discord invite code."""

    valid: bool
    group_name: str | None
    group_description: str | None
    member_count: int | None = None


async def _fetch_one(client: httpx.AsyncClient, code: str) -> InviteInfo | None:
    """Validate a single Discord invite code.

    Returns:
      - InviteInfo(valid=True, ...) — invite is live
      - InviteInfo(valid=False, ...) — invite confirmed dead (404)
      - None — transient error (timeout, 429, 5xx). Caller should retry later.
    """
    try:
        resp = await client.get(INVITE_API_URL.format(code=code))
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.info("discord validator: transient error for %s: %s", code, e)
        return None

    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception as e:
            logger.warning("discord validator: bad JSON for %s: %s", code, e)
            return InviteInfo(valid=False, group_name=None, group_description=None)
        guild = data.get("guild") or {}
        return InviteInfo(
            valid=True,
            group_name=guild.get("name"),
            group_description=guild.get("description"),
            member_count=data.get("approximate_member_count"),
        )

    if resp.status_code == 404:
        return InviteInfo(valid=False, group_name=None, group_description=None)

    if resp.status_code == 429:
        # Rate-limited — Discord includes `Retry-After`. We treat this as
        # transient so a future enrich pass retries the code.
        logger.warning("discord validator: 429 rate-limited on %s", code)
        return None

    if 500 <= resp.status_code < 600:
        return None  # transient server error

    # 4xx other than 404/429 — treat as definitive failure (e.g. 400 from a
    # malformed code). Mark dead so we don't retry forever.
    logger.info(
        "discord validator: %d for %s, marking dead", resp.status_code, code
    )
    return InviteInfo(valid=False, group_name=None, group_description=None)


async def fetch_invites(
    invite_codes: list[str],
    *,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
) -> dict[str, InviteInfo | None]:
    """Validate `invite_codes` against Discord's public API. Returns a dict
    keyed by code — None values are transient (caller should not persist).

    Raises DiscordBudgetExceeded if more codes are passed than `request_budget`.
    """
    if len(invite_codes) > request_budget:
        raise DiscordBudgetExceeded(
            f"got {len(invite_codes)} codes, budget is {request_budget}"
        )

    sem = asyncio.Semaphore(DISCORD_CONCURRENCY)

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "wp2-leadfinder (Discord invite validator)"},
    ) as client:

        async def _bounded(code: str) -> tuple[str, InviteInfo | None]:
            async with sem:
                return code, await _fetch_one(client, code)

        results = await asyncio.gather(*[_bounded(c) for c in invite_codes])

    return dict(results)
