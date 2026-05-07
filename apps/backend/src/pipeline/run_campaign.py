"""Platform dispatcher.

Single entry point for the arq worker — reads `Campaign.platform` and
delegates to the right per-platform orchestrator. Keeping this thin lets us
add new platforms (Discord, Slack, …) by writing a new orchestrator and
adding one branch here, without touching the worker registration or the
API routing layer.
"""
from __future__ import annotations

import logging

from src.db.models import Campaign
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)


async def run_campaign(campaign_id: int) -> None:
    """Read the campaign's platform and call the matching orchestrator."""
    async with SessionLocal() as s:
        campaign = await s.get(Campaign, campaign_id)
        if campaign is None:
            raise ValueError(f"campaign {campaign_id} not found")
        platform = campaign.platform

    if platform == "whatsapp":
        from src.pipeline.whatsapp.orchestrator import run_whatsapp_campaign
        await run_whatsapp_campaign(campaign_id)
        return

    if platform == "discord":
        from src.pipeline.discord.orchestrator import run_discord_campaign
        await run_discord_campaign(campaign_id)
        return

    if platform == "slack":
        from src.pipeline.slack.orchestrator import run_slack_campaign
        await run_slack_campaign(campaign_id)
        return

    # Future platforms land here. Until then, an explicit error is more
    # debuggable than a silent skip.
    raise ValueError(
        f"campaign {campaign_id}: no orchestrator registered for "
        f"platform={platform!r} (known: 'whatsapp', 'discord', 'slack')"
    )
