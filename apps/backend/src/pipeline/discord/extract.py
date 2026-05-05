"""Discord invite-link extractor (stage 5).

Mirrors the WhatsApp extractor's shape exactly — same return type, same
shared loop. The only platform-specific bits are the regex and the host
patterns it matches.
"""
from __future__ import annotations

import re

from src.pipeline.shared.extract_helpers import run_extract_for_campaign


# Discord invite-URL forms in the wild:
#   https://discord.gg/<code>             (most common)
#   https://discord.com/invite/<code>     (canonical)
#   https://discordapp.com/invite/<code>  (legacy)
#
# Discord invite codes:
#   - Auto-generated: 6-10 alphanumeric (e.g. "xK3pQrW2")
#   - Vanity (custom): 2-32 chars, alnum + `-` (e.g. "ai-agency-founders")
# We bound to {3,32} — generous for vanity codes, rejects 1-2 char fragments
# that pop up in commentary like "discord.gg/X". The trailing
# `(?![A-Za-z0-9_-])` stops over-long junk consuming only the first 32.
#
# `(?<![a-zA-Z0-9])` — same boundary guard as WhatsApp regex; rejects
# "fakediscord.gg/abc" (preceded by 'd' which would still partially match).
_INVITE_RE = re.compile(
    r"(?<![a-zA-Z0-9])"
    r"(?:discord\.gg|discord(?:app)?\.com/invite)/"
    r"([A-Za-z0-9-]{3,32})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
CONTEXT_WINDOW = 200


def extract_invites(text: str | None) -> list[tuple[str, str]]:
    """Return [(invite_code, ±200-char context), ...] for every Discord
    invite link in `text`. Dedupes within the same text — one entry per
    invite code, keeping the first context encountered.

    Codes are case-sensitive on Discord's side, so we preserve the original
    casing rather than lowercasing.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _INVITE_RE.finditer(text):
        code = m.group(1)
        if code in seen:
            continue
        seen.add(code)
        ctx_start = max(0, m.start() - CONTEXT_WINDOW)
        ctx_end = min(len(text), m.end() + CONTEXT_WINDOW)
        out.append((code, text[ctx_start:ctx_end]))
    return out


async def extract_for_campaign(campaign_id: int) -> dict[str, int]:
    """Stage 5 entrypoint for Discord campaigns. Thin wrapper that hands the
    Discord-specific extract function to the generic loop in
    `shared.extract_helpers`."""
    return await run_extract_for_campaign(campaign_id, extract_invites)
