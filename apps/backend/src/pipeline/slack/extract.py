"""Slack invite-link extractor (stage 5).

Slack identifies an invite by a `<workspace>/<token>` pair, both pieces of
which appear in the URL. We store the combined string `workspace/token` as
the lead's invite_id so:
  - The pair is uniquely identifiable across all of Slack
  - The full join URL is reconstructible (`https://join.slack.com/t/<ws>/shared_invite/<tok>`)
  - The verifier needs both pieces to call the public landing page
  - Snowball can later re-search for `"workspace"` (after enrichment confirms
    the workspace name) to find newer tokens to the same group as old ones
    expire — this matters way more for Slack than WA/Discord because Slack
    invites auto-expire in 30 days.
"""
from __future__ import annotations

import re

from src.pipeline.shared.extract_helpers import run_extract_for_campaign


# Modern Slack invite URL: https://join.slack.com/t/<workspace>/shared_invite/<token>
#   - workspace: alnum + hyphen, 3-30 chars (Slack's URL-slug rules)
#   - token:     alnum + hyphens + underscores, ~10-80 chars. Format is
#                typically `zt-<segment>-<segment>` but the segments use
#                base64url-ish alphabet that INCLUDES underscores. A real
#                example: `zt-zgitr2si-NKtdWC9IkdmvL_o4BC1kYA` — note the
#                underscore between `IkdmvL` and `o4BC1kYA`.
#
# Boundary guards:
#   `(?<![a-zA-Z0-9])` rejects "myjoin.slack.com/t/..." style lookalikes
#   `(?![A-Za-z0-9_-])` after the token stops over-long junk consuming a match
_INVITE_RE = re.compile(
    r"(?<![a-zA-Z0-9])"
    r"join\.slack\.com/t/([a-z0-9-]{3,30})/shared_invite/([A-Za-z0-9_-]{10,80})"
    r"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
CONTEXT_WINDOW = 200


def extract_invites(text: str | None) -> list[tuple[str, str]]:
    """Return [(invite_id, ±200-char context), ...] for every Slack invite link
    in `text`. invite_id is `<workspace>/<token>` — both halves combined so the
    unique constraint per (campaign, invite_id) catches duplicate discoveries
    of the same invite. Dedupes within the same text.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _INVITE_RE.finditer(text):
        workspace = m.group(1).lower()  # Slack URL slugs are case-insensitive
        token = m.group(2)              # tokens ARE case-sensitive
        invite_id = f"{workspace}/{token}"
        if invite_id in seen:
            continue
        seen.add(invite_id)
        ctx_start = max(0, m.start() - CONTEXT_WINDOW)
        ctx_end = min(len(text), m.end() + CONTEXT_WINDOW)
        out.append((invite_id, text[ctx_start:ctx_end]))
    return out


async def extract_for_campaign(campaign_id: int) -> dict[str, int]:
    """Stage 5 entrypoint for Slack campaigns. Thin wrapper that hands the
    Slack-specific extract function to the generic loop in
    `shared.extract_helpers`."""
    return await run_extract_for_campaign(campaign_id, extract_invites)
