import pytest
from sqlalchemy.exc import IntegrityError

from src.db.models import Campaign, Lead


async def test_same_invite_id_within_one_campaign_is_blocked(session):
    """UNIQUE(campaign_id, invite_id) blocks duplicate inside one campaign."""
    campaign = Campaign(name="constraint-test")
    session.add(campaign)
    await session.flush()

    session.add(Lead(invite_id="ABC123xyz", campaign_id=campaign.id))
    await session.commit()

    session.add(Lead(invite_id="ABC123xyz", campaign_id=campaign.id))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_same_invite_id_across_campaigns_is_allowed(session):
    """Cross-campaign rule changed: each campaign analyses leads fresh.
    Same invite under campaign A and campaign B = two independent rows."""
    a = Campaign(name="constraint-test-A")
    b = Campaign(name="constraint-test-B")
    session.add_all([a, b])
    await session.flush()

    session.add_all([
        Lead(invite_id="SHARED1", campaign_id=a.id),
        Lead(invite_id="SHARED1", campaign_id=b.id),
    ])
    await session.commit()  # must not raise — different campaigns


async def test_two_leads_with_different_invite_ids_coexist(session):
    campaign = Campaign(name="two-leads-test")
    session.add(campaign)
    await session.flush()

    session.add_all([
        Lead(invite_id="invite_one", campaign_id=campaign.id),
        Lead(invite_id="invite_two", campaign_id=campaign.id),
    ])
    await session.commit()  # must not raise
