import asyncio

import pytest
from sqlalchemy import func, select

from src.db.models import Campaign, Lead, Query, SearchResult, UrlCache
from src.db.session import SessionLocal
from src.pipeline.whatsapp.extract import extract_for_campaign, extract_invites


# ---------- Pure regex / context tests ----------

@pytest.mark.parametrize(
    "text,expected",
    [
        # 1. plain match
        ("join here: chat.whatsapp.com/AbCdEf123 — link", ["AbCdEf123"]),
        # 2. with https prefix
        ("https://chat.whatsapp.com/AbCdEf123 here", ["AbCdEf123"]),
        # 3. multiple distinct invites
        (
            "first chat.whatsapp.com/AbCdEf12 second chat.whatsapp.com/XyZpQr34",
            ["AbCdEf12", "XyZpQr34"],
        ),
        # 4. same invite twice → dedup within text
        (
            "chat.whatsapp.com/Foobar123 chat.whatsapp.com/Foobar123 chat.whatsapp.com/Foobar123",
            ["Foobar123"],
        ),
        # 5. trailing punctuation — period not in [A-Za-z0-9_-]
        ("chat.whatsapp.com/AbCdEf12.", ["AbCdEf12"]),
        # 6. trailing parenthesis
        ("(chat.whatsapp.com/AbCdEf12) foo", ["AbCdEf12"]),
        # 7. no invite
        ("no link here at all", []),
        # 8. malformed (no id)
        ("chat.whatsapp.com/", []),
        # 9. lookalike domain — must NOT match
        ("phishing chat.whatsapp.com.evil.com/AbCdEf12", []),
        # 10. subdomain prefix — must NOT match (preceded by 'b')
        ("subchat.whatsapp.com/AbCdEf12", []),
        # 11. case-insensitive
        ("CHAT.WHATSAPP.COM/AbCdEf12", ["AbCdEf12"]),
        # 12. dash + underscore in id
        ("chat.whatsapp.com/Abc-Def_123 here", ["Abc-Def_123"]),
        # 13. empty string
        ("", []),
        # 14. None
        (None, []),
        # 15. real-ish 22-char invite id
        ("join: chat.whatsapp.com/CXTbRkLm9YpQwX2YzAbCd2 here", ["CXTbRkLm9YpQwX2YzAbCd2"]),
        # 16. too-short id (< 6 chars) — rejected as noise/commentary fragment
        ("ping chat.whatsapp.com/X here", []),
        ("see chat.whatsapp.com/AbCd link", []),
        # 17. too-long id (> 30 chars) — rejected as junk string
        ("chat.whatsapp.com/" + "A" * 50 + " end", []),
    ],
)
def test_extract_invites(text, expected):
    out = [iid for (iid, _ctx) in extract_invites(text)]
    assert out == expected, f"text={text!r}"


def test_extract_invites_includes_context_window():
    text = "x" * 300 + "see chat.whatsapp.com/AbCdEf12 ok" + "y" * 300
    [(invite_id, ctx)] = extract_invites(text)
    assert invite_id == "AbCdEf12"
    # context should be ~ ±200 chars around the match (plus the match itself)
    assert "chat.whatsapp.com/AbCdEf12" in ctx
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


# ---------- Source-recurrence: distinct-URL counting ----------


async def test_source_count_increments_on_distinct_urls_only():
    """An invite_id discovered on three different URLs should end with
    source_count=3. Re-extracting the SAME URL (e.g. on idempotent re-run)
    must NOT bump the counter."""
    from src.db.models import LeadSourceUrl

    cid = await _seed_campaign(name="step8-source-count")
    qid = await _seed_query(cid)

    invite = "RecurringInvite99"
    md = f"share: chat.whatsapp.com/{invite} — join the group"

    urls = [
        "https://reddit.com/r/x/post1",
        "https://reddit.com/r/y/post2",
        "https://blog.example.com/article",
    ]

    async with SessionLocal() as s:
        for u in urls:
            s.add(UrlCache(url=u, markdown=md))
            s.add(
                SearchResult(
                    query_id=qid,
                    url=u,
                    status="fetched",
                    fetch_strategy="reddit" if "reddit.com" in u else "web",
                )
            )
        await s.commit()

    # First extraction pass — should count 3 distinct URLs.
    await extract_for_campaign(cid)

    async with SessionLocal() as s:
        lead = (
            await s.execute(select(Lead).where(Lead.invite_id == invite))
        ).scalar_one()
        url_rows = list(
            (
                await s.execute(
                    select(LeadSourceUrl.url).where(LeadSourceUrl.lead_id == lead.id)
                )
            ).scalars()
        )

    assert lead.source_count == 3, f"expected source_count=3, got {lead.source_count}"
    assert set(url_rows) == set(urls), f"junction table mismatch: {url_rows}"

    # Idempotent re-run — same rows already 'extracted', no new junction
    # entries, source_count must stay at 3.
    await extract_for_campaign(cid)
    async with SessionLocal() as s:
        lead = (
            await s.execute(select(Lead).where(Lead.invite_id == invite))
        ).scalar_one()
    assert lead.source_count == 3, "re-run must not bump source_count"


async def test_source_count_defaults_to_one_for_single_source():
    """A vanilla single-URL discovery should land with source_count=1."""
    cid = await _seed_campaign(name="step8-single-source")
    qid = await _seed_query(cid)

    async with SessionLocal() as s:
        s.add(
            SearchResult(
                query_id=qid,
                url="https://reddit.com/r/x/y",
                snippet="see chat.whatsapp.com/SoloInvite12 here",
                status="new",
                fetch_strategy="snippet_hit",
            )
        )
        await s.commit()

    await extract_for_campaign(cid)

    async with SessionLocal() as s:
        lead = (
            await s.execute(select(Lead).where(Lead.invite_id == "SoloInvite12"))
        ).scalar_one()
    assert lead.source_count == 1
