"""Discord stage 5 — invite extraction tests (regex + DB integration)."""
import pytest
from sqlalchemy import select

from src.db.models import Campaign, Lead, Query, SearchResult, UrlCache
from src.db.session import SessionLocal
from src.pipeline.discord.extract import extract_for_campaign, extract_invites


# ---------- Pure regex tests ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Standard discord.gg URL
        ("join here: discord.gg/xK3pQrW2 — link", ["xK3pQrW2"]),
        # With https prefix
        ("https://discord.gg/xK3pQrW2 here", ["xK3pQrW2"]),
        # Canonical discord.com/invite form
        ("see https://discord.com/invite/AbCd1234 below", ["AbCd1234"]),
        # Legacy discordapp.com/invite form
        ("old https://discordapp.com/invite/MyCode99 here", ["MyCode99"]),
        # Vanity code with hyphen
        ("vanity discord.gg/ai-agency-founders link", ["ai-agency-founders"]),
        # Multiple distinct invites in one text
        (
            "first discord.gg/AbCdEf12 second discord.com/invite/XyZ78901",
            ["AbCdEf12", "XyZ78901"],
        ),
        # Same code twice → dedup within text
        (
            "discord.gg/Foobar123 discord.gg/Foobar123 discord.gg/Foobar123",
            ["Foobar123"],
        ),
        # Trailing punctuation — period not in [A-Za-z0-9_-]
        ("discord.gg/AbCdEf12.", ["AbCdEf12"]),
        # Trailing parenthesis
        ("(discord.gg/AbCdEf12) foo", ["AbCdEf12"]),
        # No invite
        ("no link here at all", []),
        # Malformed (no code)
        ("discord.gg/", []),
        # Lookalike domain — must NOT match
        ("phishing fakediscord.gg/AbCdEf12", []),
        # Subdomain prefix — must NOT match (preceded by 'd')
        ("baddiscord.gg/Abc12345", []),
        # Case-insensitive host
        ("DISCORD.GG/AbCdEf12", ["AbCdEf12"]),
        # Empty / None
        ("", []),
        (None, []),
        # Too-short code (< 3 chars) — rejected as noise
        ("ping discord.gg/X here", []),
        ("see discord.gg/Ab link", []),
        # Too-long code (> 32 chars) — rejected as junk
        ("discord.gg/" + "A" * 50 + " end", []),
        # Cross-platform leak: WhatsApp invite must NOT match Discord regex
        ("see chat.whatsapp.com/AbCdEf123456 here", []),
    ],
)
def test_extract_discord_invites(text, expected):
    out = [iid for (iid, _ctx) in extract_invites(text)]
    assert out == expected, f"text={text!r}"


def test_discord_extract_includes_context_window():
    text = "x" * 300 + "see discord.gg/AbCdEf12 ok" + "y" * 300
    [(code, ctx)] = extract_invites(text)
    assert code == "AbCdEf12"
    assert "discord.gg/AbCdEf12" in ctx
    assert 200 <= len(ctx) <= 600


# ---------- Integration: DB-only ----------


async def _seed_campaign(name: str = "step8-discord") -> int:
    async with SessionLocal() as s:
        c = Campaign(name=name, industries=["x"], platform="discord")
        s.add(c)
        await s.commit()
        return c.id


async def _seed_query(campaign_id: int) -> int:
    async with SessionLocal() as s:
        q = Query(campaign_id=campaign_id, query_text="probe", status="searched")
        s.add(q)
        await s.commit()
        return q.id


async def test_snippet_hit_creates_discord_lead_from_snippet_text():
    cid = await _seed_campaign()
    qid = await _seed_query(cid)

    async with SessionLocal() as s:
        s.add(
            SearchResult(
                query_id=qid,
                url="https://reddit.com/r/agency/x",
                title="AI agency owners discord",
                snippet="join us: discord.gg/SnippetServer42",
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
    assert leads[0].invite_id == "SnippetServer42"


async def test_fetched_page_creates_discord_lead_from_cached_markdown():
    cid = await _seed_campaign()
    qid = await _seed_query(cid)

    url = "https://reddit.com/r/agency/post123"
    md = (
        "# Some thread\n\nbla bla\n\n"
        "join: https://discord.com/invite/CachedServer99\n\nmore text"
    )

    async with SessionLocal() as s:
        s.add(UrlCache(url=url, markdown=md))
        s.add(
            SearchResult(
                query_id=qid,
                url=url,
                title="Some thread",
                snippet="bla",
                status="fetched",
                fetch_strategy="reddit",
            )
        )
        await s.commit()

    counts = await extract_for_campaign(cid)
    assert counts["new_leads"] == 1

    async with SessionLocal() as s:
        leads = list((await s.execute(select(Lead).where(Lead.campaign_id == cid))).scalars())
    assert leads[0].invite_id == "CachedServer99"


async def test_discord_extractor_ignores_whatsapp_invites_in_same_text():
    """A page mentioning BOTH a WA invite and a Discord invite, scanned by
    the Discord extractor, must produce only the Discord lead. Cross-platform
    leakage is the most important correctness property here — without it,
    a Discord campaign would store WA invites under Discord codes and the
    Discord validator would mark them all dead."""
    cid = await _seed_campaign()
    qid = await _seed_query(cid)

    url = "https://blog.example.com/list"
    md = (
        "## Best community links\n"
        "- WhatsApp: chat.whatsapp.com/WhatsAppCode22\n"
        "- Discord: discord.gg/DiscordCode42\n"
    )

    async with SessionLocal() as s:
        s.add(UrlCache(url=url, markdown=md))
        s.add(
            SearchResult(
                query_id=qid,
                url=url,
                status="fetched",
                fetch_strategy="web",
            )
        )
        await s.commit()

    await extract_for_campaign(cid)

    async with SessionLocal() as s:
        leads = list((await s.execute(select(Lead).where(Lead.campaign_id == cid))).scalars())
    invite_ids = sorted(l.invite_id for l in leads)
    assert invite_ids == ["DiscordCode42"], (
        f"expected only Discord lead, got {invite_ids}"
    )
