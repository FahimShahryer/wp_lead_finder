"""WhatsApp invite-link extractor (stage 5).

The platform-specific bit is just the regex; everything else (search_result
iteration, lead upsert, source-URL junction maintenance) lives in
`shared/extract_helpers.py`.
"""
from __future__ import annotations

import re

from src.pipeline.shared.extract_helpers import run_extract_for_campaign

# `(?<![a-zA-Z0-9])` — must not be preceded by a letter/digit.
# This rejects "subchat.whatsapp.com/X" (preceded by 'b') while accepting
# "https://chat.whatsapp.com/X" (preceded by '/').
#
# `{6,30}` — invite_id length bound. Real WhatsApp invite_ids are 22 chars
# (post-2018 format); the `{6,30}` band keeps a safety buffer for any future
# format change while rejecting trivial false-positive matches like
# `chat.whatsapp.com/x` or `chat.whatsapp.com/abc` that occur in commentary,
# truncated URLs, or partial scrapes. The trailing `(?![A-Za-z0-9_-])` guard
# stops the regex consuming only the FIRST 30 chars of a longer junk string.
_INVITE_RE = re.compile(
    r"(?<![a-zA-Z0-9])chat\.whatsapp\.com/([A-Za-z0-9_-]{6,30})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
CONTEXT_WINDOW = 200


def extract_invites(text: str | None) -> list[tuple[str, str]]:
    """Return [(invite_id, ±200-char context), ...] for every WA invite link
    in `text`. Dedupes within the same text — one entry per invite_id, keeping
    the first context encountered.
    """
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


async def extract_for_campaign(campaign_id: int) -> dict[str, int]:
    """Stage 5 entrypoint for WhatsApp campaigns. Thin wrapper that hands the
    WA-specific extract function to the generic loop in
    `shared.extract_helpers`."""
    return await run_extract_for_campaign(campaign_id, extract_invites)
