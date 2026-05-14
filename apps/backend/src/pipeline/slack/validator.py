"""Slack invite validator — fetches the public landing page and parses it
for live/dead status + the workspace name.

Why this is fundamentally different from WhatsApp / Discord:

  - No public API. Slack does not expose a /v1/invites/{token} endpoint
    the way Discord does. The only signal source is the rendered HTML page.
  - Slack always returns HTTP 200 — even for expired invites. They show
    a friendly "this invite link is no longer valid" page. So status_code
    alone tells us nothing; we MUST inspect the body.
  - Workspace size is private. Unlike Discord where we get
    `approximate_member_count`, Slack hides this on the invite page.
    Score signals come from group_name + source context only.
  - Tokens auto-expire ~30 days from creation. Expect 30-50% "dead" rate
    on any campaign older than a week — this is normal, not a bug.

Strategy:
  1. GET the join URL with a polite User-Agent and short timeout
  2. If body contains a "no longer valid" / "invite has expired" / similar
     marker → invite is dead
  3. Otherwise scrape <title> and og:site_name to recover the workspace name
  4. Anything else (timeouts, 5xx, network blips) → transient, caller
     decides whether to retry or mark
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


# Slack invite landing pages are public HTML, not an API endpoint. Be polite:
# Sem(8) + a small base delay yields ~25/sec peak — well under any rate limit
# Slack might enforce on their public CDN.
SLACK_CONCURRENCY = 8
PER_REQUEST_DELAY = 0.3
REQUEST_TIMEOUT = 12.0
DEFAULT_REQUEST_BUDGET = 200

# Construct the canonical join URL from a workspace + token pair.
INVITE_URL_TEMPLATE = "https://join.slack.com/t/{workspace}/shared_invite/{token}"

# Phrases that appear in Slack's "this invite is dead" landing page. Slack
# has changed the wording over the years; we match all the variants we've
# seen. Case-insensitive substring match.
_DEAD_PHRASES: tuple[str, ...] = (
    "this invite link is no longer valid",
    "this link is no longer active",
    "this link has expired",
    "this invite has expired",
    "this link can't be used",
    "invite link is no longer valid",
    "invite link is invalid",
    "this invitation is no longer active",
    "no longer accepting new members",
    "invitation has expired",
)

# Patterns that capture the workspace name from a live invite page. Slack's
# landing page sets the document title to "Join <Workspace> on Slack" and
# also exposes og:site_name and og:title. We try them in order.
_TITLE_RE = re.compile(
    r"<title[^>]*>\s*(?:Join\s+)?([^<|]+?)\s*(?:\bon\s+Slack)?\s*</title>",
    re.IGNORECASE,
)
_OG_SITE_NAME_RE = re.compile(
    r"""<meta\s+(?:[^>]*?\s+)?property=["']og:site_name["']\s+content=["']([^"']+)["']""",
    re.IGNORECASE,
)
_OG_TITLE_RE = re.compile(
    r"""<meta\s+(?:[^>]*?\s+)?property=["']og:title["']\s+content=["']([^"']+)["']""",
    re.IGNORECASE,
)


class SlackBudgetExceeded(Exception):
    """Raised when caller asks for more invites than `request_budget`."""


@dataclass
class InviteInfo:
    """Result of validating one Slack invite."""

    valid: bool
    group_name: str | None
    group_description: str | None = None  # Slack landing pages don't expose this


def _split_invite_id(invite_id: str) -> tuple[str, str] | None:
    """Slack invite_ids are stored as `<workspace>/<token>`. Split into the
    pair, or None if malformed (defensive — the extractor produces the
    canonical form, but a hand-edited row could be wrong)."""
    if "/" not in invite_id:
        return None
    workspace, _, token = invite_id.partition("/")
    if not workspace or not token:
        return None
    return workspace, token


def _looks_dead(body_lower: str) -> bool:
    return any(phrase in body_lower for phrase in _DEAD_PHRASES)


def _parse_workspace_name(body: str, fallback_workspace: str) -> str | None:
    """Pull the human-readable workspace name out of the page HTML.
    Falls back to the URL-slug workspace if scraping fails."""
    for rx in (_OG_SITE_NAME_RE, _OG_TITLE_RE, _TITLE_RE):
        m = rx.search(body)
        if not m:
            continue
        name = m.group(1).strip()
        # Strip "Join " prefix and " on Slack" suffix that some templates use.
        name = re.sub(r"^Join\s+", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\s*on\s+Slack\s*$", "", name, flags=re.IGNORECASE)
        name = name.strip()
        if name and len(name) <= 200:
            return name
    # Fall back to the URL slug — better than nothing, lets snowball
    # search work even when scraping fails.
    return fallback_workspace.replace("-", " ").title() or None


async def _fetch_one(
    client: httpx.AsyncClient, invite_id: str
) -> InviteInfo | None:
    """Validate a single Slack invite_id (`workspace/token`).

    Returns:
      - InviteInfo(valid=True, ...) — invite is live; workspace name extracted
      - InviteInfo(valid=False, ...) — invite confirmed dead by page content
      - None — transient (timeout, 5xx, parse error). Caller should not persist.
    """
    parts = _split_invite_id(invite_id)
    if parts is None:
        # Malformed invite_id — treat as definitively bad so we don't retry.
        logger.info("slack validator: malformed invite_id %r", invite_id)
        return InviteInfo(valid=False, group_name=None)
    workspace, token = parts
    url = INVITE_URL_TEMPLATE.format(workspace=workspace, token=token)

    try:
        await asyncio.sleep(PER_REQUEST_DELAY)
        resp = await client.get(url, follow_redirects=True)
    except (httpx.TimeoutException, httpx.RequestError) as e:
        logger.info("slack validator: transient error for %s: %s", invite_id, e)
        return None

    if 500 <= resp.status_code < 600:
        return None  # transient server error

    if resp.status_code in (401, 403):
        # Slack uses 401/403 for "you need to sign in to see this content"
        # which means the workspace exists but the invite isn't open. Treat
        # as definitively dead from our prospecting POV — we can't join.
        return InviteInfo(valid=False, group_name=None)

    if resp.status_code == 404:
        return InviteInfo(valid=False, group_name=None)

    if resp.status_code != 200:
        # Other 4xx: be conservative and mark dead.
        return InviteInfo(valid=False, group_name=None)

    body = resp.text
    if _looks_dead(body.lower()):
        return InviteInfo(valid=False, group_name=None)

    name = _parse_workspace_name(body, workspace)
    return InviteInfo(valid=True, group_name=name)


async def fetch_invites(
    invite_ids: list[str],
    *,
    request_budget: int = DEFAULT_REQUEST_BUDGET,
) -> dict[str, InviteInfo | None]:
    """Validate a batch of Slack invite_ids. Returns a dict keyed by invite_id.
    None values are transient — caller must not persist them.

    Raises SlackBudgetExceeded if more invites are passed than the budget.
    """
    if len(invite_ids) > request_budget:
        raise SlackBudgetExceeded(
            f"got {len(invite_ids)} invites, budget is {request_budget}"
        )

    sem = asyncio.Semaphore(SLACK_CONCURRENCY)

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={
            # Slack returns HTTP 500 (with a JS-rendered page body) for any
            # User-Agent that contains a `(...)` parenthesized comment — even
            # `Mozilla/5.0 (compatible; wp2-leadfinder/...)`. A bare `Mozilla/5.0`
            # gets the actual status (200 for live invites, 403 for expired,
            # 404 for nonexistent). curl/* and Firefox/* both work too. We pick
            # the bare form because it's the most stable across UA-sniffing
            # changes Slack might make.
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as client:

        async def _bounded(invite_id: str) -> tuple[str, InviteInfo | None]:
            async with sem:
                return invite_id, await _fetch_one(client, invite_id)

        results = await asyncio.gather(*[_bounded(i) for i in invite_ids])

    return dict(results)
