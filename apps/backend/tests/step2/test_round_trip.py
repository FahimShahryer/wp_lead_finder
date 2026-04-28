from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.db.models import Campaign, Lead, Query, SearchResult, UrlCache
from src.db.session import SessionLocal


async def test_campaign_round_trip_and_relationships(session):
    campaign = Campaign(
        name="agency owners",
        industries=["AI agency owners", "Marketing agency owners"],
        locations=["US", "UK", "Dubai"],
        negative_locations=["India", "Indian"],
        platforms=["reddit", "web"],
    )
    session.add(campaign)
    await session.flush()

    q = Query(
        campaign_id=campaign.id,
        query_text='site:reddit.com whatsapp "AI agency" US -india',
        source_platform="reddit",
    )
    session.add(q)
    await session.flush()

    sr = SearchResult(
        query_id=q.id,
        url="https://reddit.com/r/agency/comments/abc",
        title="AI Agency Owners — share invites",
        snippet="join our whatsapp group chat.whatsapp.com/AbCdEf123",
        position=3,
    )
    session.add(sr)

    lead = Lead(
        invite_id="AbCdEf123",
        campaign_id=campaign.id,
        source_url="https://reddit.com/r/agency/comments/abc",
        source_text="join our whatsapp group chat.whatsapp.com/AbCdEf123 ...",
    )
    session.add(lead)
    await session.commit()

    stmt = (
        select(Campaign)
        .where(Campaign.id == campaign.id)
        .options(
            selectinload(Campaign.queries).selectinload(Query.search_results),
            selectinload(Campaign.leads),
        )
    )
    fetched = (await session.execute(stmt)).scalar_one()

    assert fetched.name == "agency owners"
    assert fetched.industries == ["AI agency owners", "Marketing agency owners"]
    assert fetched.locations == ["US", "UK", "Dubai"]
    assert fetched.negative_locations == ["India", "Indian"]
    assert fetched.status == "queued"  # python-side default applied on insert
    assert fetched.created_at is not None

    assert len(fetched.queries) == 1
    assert fetched.queries[0].query_text.startswith("site:reddit.com")
    assert fetched.queries[0].status == "pending"

    assert len(fetched.queries[0].search_results) == 1
    assert fetched.queries[0].search_results[0].title.startswith("AI Agency")

    assert len(fetched.leads) == 1
    assert fetched.leads[0].invite_id == "AbCdEf123"


async def test_cascade_delete_removes_queries_search_results_leads(session):
    campaign = Campaign(name="cascade-test")
    session.add(campaign)
    await session.flush()

    q = Query(campaign_id=campaign.id, query_text="probe")
    session.add(q)
    await session.flush()

    sr = SearchResult(query_id=q.id, url="https://example.com/x")
    lead = Lead(invite_id="cascade-invite", campaign_id=campaign.id)
    session.add_all([sr, lead])
    await session.commit()

    q_id, sr_id, lead_id = q.id, sr.id, lead.id

    await session.delete(campaign)
    await session.commit()

    # Use a fresh session: passive_deletes=True trusts the DB FK CASCADE,
    # so the original session's identity map still holds the (now-deleted) rows.
    async with SessionLocal() as fresh:
        assert (await fresh.get(Query, q_id)) is None
        assert (await fresh.get(SearchResult, sr_id)) is None
        assert (await fresh.get(Lead, lead_id)) is None


async def test_url_cache_pk_and_round_trip(session):
    cached = UrlCache(
        url="https://reddit.com/r/agency/x",
        markdown="# AI Agency Group\n\nchat.whatsapp.com/AbCdEf123",
    )
    session.add(cached)
    await session.commit()

    fetched = await session.get(UrlCache, "https://reddit.com/r/agency/x")
    assert fetched is not None
    assert "chat.whatsapp.com/AbCdEf123" in fetched.markdown
    assert fetched.fetched_at is not None
