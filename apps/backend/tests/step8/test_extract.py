import asyncio

import pytest
from sqlalchemy import func, select

from src.db.models import Campaign, Lead, Query, SearchResult, UrlCache
from src.db.session import SessionLocal
from src.pipeline.stage5_extract import extract_for_campaign, extract_invites


# ---------- Pure regex / context tests ----------

@pytest.mark.parametrize(
    "text,expected",
    [
        # 1. plain match
        ("join here: chat.whatsapp.com/AbCdEf123 — link", ["AbCdEf123"]),
        # 2. with https prefix
        ("https://chat.whatsapp.com/AbCdEf123 here", ["AbCdEf123"]),
        # 3. multiple distinct invites
        ("first chat.whatsapp.com/A1 second chat.whatsapp.com/B2", ["A1", "B2"]),
        # 4. same invite twice → dedup within text
        ("chat.whatsapp.com/X chat.whatsapp.com/X chat.whatsapp.com/X", ["X"]),
        # 5. trailing punctuation — period not in [A-Za-z0-9_-]
        ("chat.whatsapp.com/AbCd.", ["AbCd"]),
        # 6. trailing parenthesis
        ("(chat.whatsapp.com/AbCd) foo", ["AbCd"]),
        # 7. no invite
        ("no link here at all", []),
        # 8. malformed (no id)
        ("chat.whatsapp.com/", []),
        # 9. lookalike domain — must NOT match
        ("phishing chat.whatsapp.com.evil.com/Abc", []),
        # 10. subdomain prefix — must NOT match (preceded by 'b')
        ("subchat.whatsapp.com/Abc", []),
        # 11. case-insensitive
        ("CHAT.WHATSAPP.COM/AbCd", ["AbCd"]),
        # 12. dash + underscore in id
        ("chat.whatsapp.com/Abc-Def_123 here", ["Abc-Def_123"]),
        # 13. empty string
        ("", []),
        # 14. None
        (None, []),
        # 15. real-ish 22-char invite id
        ("join: chat.whatsapp.com/CXTbRkLm9YpQwX2YzAbCd2 here", ["CXTbRkLm9YpQwX2YzAbCd2"]),
    ],
)
def test_extract_invites(text, expected):
    out = [iid for (iid, _ctx) in extract_invites(text)]
    assert out == expected, f"text={text!r}"


def test_extract_invites_includes_context_window():
    text = "x" * 300 + "see chat.whatsapp.com/AbCd ok" + "y" * 300
    [(invite_id, ctx)] = extract_invites(text)
    assert invite_id == "AbCd"
    # context should be ~ ±200 chars around the match (plus the match itself)
    assert "chat.whatsapp.com/AbCd" in ctx
    assert 200 <= len(ctx) <= 600


# ---------- Integration tests ----------

async def _seed_campaign(name="step8") -> int:
    async with SessionLocal() as s:
        c = Campaign(name=name, industries=["x"])
        s.add(c)
        await s.commit()
        return c.id


async def _seed_query(campaign_id: int) -> int:
    async with SessionLocal() as s:
        q = Query(campaign_id=campaign_id, query_text="probe", status="searched")
        s.add(q)
        await s.commit()
        return q.id


async def test_snippet_hit_creates_lead_from_snippet_text():
    cid = await _seed_campaign()
    qid = await _seed_query(cid)

    async with SessionLocal() as s:
        s.add(
            SearchResult(
                query_id=qid,
                url="https://reddit.com/r/agency/x",
                title="AI agency owners group",
                snippet="join us: chat.whatsapp.com/SnippetInvite42",
                status="new",
                fetch_strategy="snippet_hit",
            )
        )
        await s.commit()

    counts = await extract_for_campaign(cid)
    assert counts["new_leads"] == 1
    assert counts["extracted_rows"] == 1

    async with SessionLocal() as s:
        leads = list((await s.execute(select(Lead).where(Lead.campaign_id == cid))).scalars())
    assert len(leads) == 1
    assert leads[0].invite_id == "SnippetInvite42"
    assert "chat.whatsapp.com/SnippetInvite42" in leads[0].source_text


async def test_fetched_row_creates_lead_from_url_cache_markdown():
    cid = await _seed_campaign()
    qid = await _seed_query(cid)

    url = "https://reddit.com/r/agency/post123"
    md = "# Some thread\n\nbla bla\n\njoin: chat.whatsapp.com/CachedInvite99\n\nmore text"

    async with SessionLocal() as s:
        s.add(UrlCache(url=url, markdown=md))
        s.add(
            SearchResult(
                query_id=qid,
                url=url,
                title="Some thread",
                snippet="bla",  # snippet does NOT contain the invite
                status="fetched",  # already fetched by stage 4a
                fetch_strategy="reddit",
            )
        )
        await s.commit()

    counts = await extract_for_campaign(cid)
    assert counts["new_leads"] == 1

    async with SessionLocal() as s:
        leads = list((await s.execute(select(Lead).where(Lead.campaign_id == cid))).scalars())
    assert len(leads) == 1
    assert leads[0].invite_id == "CachedInvite99"


async def test_same_invite_id_across_two_campaigns_creates_independent_leads():
    """Per-campaign analysis: same invite_id discovered by campaign A AND B
    produces TWO lead rows, each owned by its own campaign and scored
    independently. (Cache reuse still saves Firecrawl credits at the URL layer
    — just not at the leads layer.)"""
    invite = "SharedInvite7777"
    md = f"check chat.whatsapp.com/{invite} please"

    # Campaign 1
    c1 = await _seed_campaign(name="step8-c1")
    q1 = await _seed_query(c1)
    url1 = "https://reddit.com/r/x/post1"
    async with SessionLocal() as s:
        s.add(UrlCache(url=url1, markdown=md))
        s.add(SearchResult(query_id=q1, url=url1, status="fetched", fetch_strategy="reddit"))
        await s.commit()

    counts1 = await extract_for_campaign(c1)
    assert counts1["new_leads"] == 1

    # Campaign 2 — different source URL, same invite
    c2 = await _seed_campaign(name="step8-c2")
    q2 = await _seed_query(c2)
    url2 = "https://forum.example.com/post-77"
    async with SessionLocal() as s:
        s.add(UrlCache(url=url2, markdown=md))
        s.add(SearchResult(query_id=q2, url=url2, status="fetched", fetch_strategy="web"))
        await s.commit()

    counts2 = await extract_for_campaign(c2)
    assert counts2["new_leads"] == 1, "campaign 2 sees this as a fresh discovery"
    assert counts2["updated_leads"] == 0

    async with SessionLocal() as s:
        rows = list(
            (await s.execute(select(Lead).where(Lead.invite_id == invite))).scalars()
        )
    assert len(rows) == 2, "two independent rows expected (one per campaign)"
    by_campaign = {l.campaign_id: l for l in rows}
    assert set(by_campaign.keys()) == {c1, c2}
    # Each row records its own discovery URL
    assert by_campaign[c1].source_url == url1
    assert by_campaign[c2].source_url == url2


async def test_idempotent_rerun_does_not_create_or_touch_leads():
    cid = await _seed_campaign()
    qid = await _seed_query(cid)

    async with SessionLocal() as s:
        s.add(
            SearchResult(
                query_id=qid,
                url="https://reddit.com/r/x/y",
                snippet="link: chat.whatsapp.com/Idempotent11",
                status="new",
                fetch_strategy="snippet_hit",
            )
        )
        await s.commit()

    first = await extract_for_campaign(cid)
    async with SessionLocal() as s:
        ls_after_first = (
            await s.execute(select(Lead).where(Lead.invite_id == "Idempotent11"))
        ).scalar_one().last_seen

    second = await extract_for_campaign(cid)
    async with SessionLocal() as s:
        ls_after_second = (
            await s.execute(select(Lead).where(Lead.invite_id == "Idempotent11"))
        ).scalar_one().last_seen

    assert first["new_leads"] == 1
    assert second == {
        "new_leads": 0, "updated_leads": 0, "extracted_rows": 0, "no_invite": 0
    }
    assert ls_after_first == ls_after_second, "idempotent re-run must not bump last_seen"


async def test_no_invite_in_content_marks_extracted_without_creating_lead():
    cid = await _seed_campaign()
    qid = await _seed_query(cid)

    async with SessionLocal() as s:
        s.add(UrlCache(url="https://example.com/empty", markdown="# nothing whatsapp here"))
        s.add(
            SearchResult(
                query_id=qid,
                url="https://example.com/empty",
                status="fetched",
                fetch_strategy="web",
            )
        )
        await s.commit()

    counts = await extract_for_campaign(cid)
    assert counts == {"new_leads": 0, "updated_leads": 0, "extracted_rows": 0, "no_invite": 1}

    async with SessionLocal() as s:
        sr = (await s.execute(select(SearchResult))).scalar_one()
    assert sr.status == "extracted"
