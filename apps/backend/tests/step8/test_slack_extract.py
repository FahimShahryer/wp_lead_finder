"""Slack stage 5 — invite extraction tests (regex + DB integration)."""
import pytest
from sqlalchemy import select

from src.db.models import Campaign, Lead, Query, SearchResult, UrlCache
from src.db.session import SessionLocal
from src.pipeline.slack.extract import extract_for_campaign, extract_invites


# ---------- Pure regex tests ----------


@pytest.mark.parametrize(
    "text,expected",
    [
        # Standard Slack invite URL
        (
            "join us: https://join.slack.com/t/marketing-agencies/shared_invite/zt-abc123-XYZ45678",
            ["marketing-agencies/zt-abc123-XYZ45678"],
        ),
        # No https prefix
        (
            "join.slack.com/t/demand-curve/shared_invite/zt-1abc23def-XYZ",
            ["demand-curve/zt-1abc23def-XYZ"],
        ),
        # Multiple invites in one text
        (
            "first join.slack.com/t/aaa/shared_invite/zt-tok1-foobar second "
            "https://join.slack.com/t/bbb/shared_invite/zt-tok2-barbaz",
            ["aaa/zt-tok1-foobar", "bbb/zt-tok2-barbaz"],
        ),
        # Same invite twice → dedup within text
        (
            "join.slack.com/t/foo/shared_invite/zt-AAA111-BBB join.slack.com/t/foo/shared_invite/zt-AAA111-BBB",
            ["foo/zt-AAA111-BBB"],
        ),
        # Trailing punctuation
        (
            "Visit https://join.slack.com/t/marketing/shared_invite/zt-abc-XYZ.",
            ["marketing/zt-abc-XYZ"],
        ),
        # Trailing parenthesis (markdown link form)
        (
            "[click here](https://join.slack.com/t/saas-club/shared_invite/zt-1abc-XYZ45678)",
            ["saas-club/zt-1abc-XYZ45678"],
        ),
        # Workspace name has hyphens (common case)
        (
            "join.slack.com/t/ai-marketing-network/shared_invite/zt-1xyz23-AAABBBCCC",
            ["ai-marketing-network/zt-1xyz23-AAABBBCCC"],
        ),
        # No invite
        ("no link here at all", []),
        # Malformed (missing token)
        ("join.slack.com/t/foo/shared_invite/", []),
        # Lookalike domain — must NOT match
        ("phishing notjoin.slack.com/t/foo/shared_invite/zt-tok-xxx", []),
        # Subdomain prefix — must NOT match (preceded by alnum)
        (
            "fakejoin.slack.com/t/foo/shared_invite/zt-tok-xxx",
            [],
        ),
        # Empty / None
        ("", []),
        (None, []),
        # Token too short (< 10 chars after `zt-` minimum) — should not match
        ("join.slack.com/t/foo/shared_invite/short", []),
        # Cross-platform leak: Discord URL must NOT match Slack regex
        ("see discord.gg/AbCdEf12 here", []),
        # Cross-platform leak: WhatsApp URL must NOT match Slack regex
        ("see chat.whatsapp.com/AbCdEf123456 here", []),
    ],
)
def test_extract_slack_invites(text, expected):
    out = [iid for (iid, _ctx) in extract_invites(text)]
    assert out == expected, f"text={text!r}"


def test_slack_invite_id_format_is_workspace_slash_token():
    """Invite_id is `<workspace>/<token>` — both halves combined so the
    URL can be reconstructed from the lead row alone."""
    text = "see https://join.slack.com/t/demand-curve/shared_invite/zt-foo123-AAABBBCCC"
    [(invite_id, _ctx)] = extract_invites(text)
    workspace, _, token = invite_id.partition("/")
    assert workspace == "demand-curve"
    assert token == "zt-foo123-AAABBBCCC"


def test_slack_workspace_lowercased():
    """Slack URL slugs are case-insensitive — we normalize to lowercase so
    'Demand-Curve' and 'demand-curve' don't create two leads."""
    [(invite_id, _ctx)] = extract_invites(
        "join.slack.com/t/Demand-Curve/shared_invite/zt-AAA111-BBB"
    )
    assert invite_id.startswith("demand-curve/")


def test_slack_token_case_preserved():
    """Tokens ARE case-sensitive on Slack's side — we must NOT normalize
    them, or the validator's reconstructed URL would 404."""
    [(invite_id, _ctx)] = extract_invites(
        "join.slack.com/t/foo/shared_invite/zt-AbC123-XyZ456789"
    )
    assert invite_id.endswith("/zt-AbC123-XyZ456789")


# ---------- Integration: DB-only ----------


async def _seed_campaign(name: str = "step8-slack") -> int:
    async with SessionLocal() as s:
        c = Campaign(name=name, industries=["x"], platform="slack")
        s.add(c)
        await s.commit()
        return c.id


async def _seed_query(campaign_id: int) -> int:
    async with SessionLocal() as s:
        q = Query(campaign_id=campaign_id, query_text="probe", status="searched")
        s.add(q)
        await s.commit()
        return q.id


async def test_snippet_hit_creates_slack_lead_from_snippet_text():
    cid = await _seed_campaign()
    qid = await _seed_query(cid)

    async with SessionLocal() as s:
        s.add(
            SearchResult(
                query_id=qid,
                url="https://blog.example.com/community",
                title="Marketing community",
                snippet="join us: https://join.slack.com/t/marketing-club/shared_invite/zt-snip12-ABCDEF",
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
    assert leads[0].invite_id == "marketing-club/zt-snip12-ABCDEF"


async def test_fetched_page_creates_slack_lead_from_cached_markdown():
    cid = await _seed_campaign()
    qid = await _seed_query(cid)

    url = "https://www.indiehackers.com/post/launch-day"
    md = (
        "# We launched today!\n\n"
        "Come hang out: [join the Slack](https://join.slack.com/t/launch-club/shared_invite/zt-cached99-XYZ123)\n"
    )

    async with SessionLocal() as s:
        s.add(UrlCache(url=url, markdown=md))
        s.add(
            SearchResult(
                query_id=qid,
                url=url,
                title="We launched today",
                snippet="bla",
                status="fetched",
                fetch_strategy="web",
            )
        )
        await s.commit()

    counts = await extract_for_campaign(cid)
    assert counts["new_leads"] == 1

    async with SessionLocal() as s:
        leads = list((await s.execute(select(Lead).where(Lead.campaign_id == cid))).scalars())
    assert leads[0].invite_id == "launch-club/zt-cached99-XYZ123"


async def test_slack_extractor_ignores_other_platform_invites_in_same_text():
    """Cross-platform leak guard: a page mentioning WA + Discord + Slack invites
    scanned by the Slack extractor must produce only the Slack lead."""
    cid = await _seed_campaign()
    qid = await _seed_query(cid)

    url = "https://blog.example.com/communities-list"
    md = (
        "## Best B2B communities\n"
        "- WhatsApp: chat.whatsapp.com/WhatsAppCode22\n"
        "- Discord: discord.gg/DiscordCode42\n"
        "- Slack: https://join.slack.com/t/saas-pros/shared_invite/zt-slacktok-AAAA1234\n"
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
    assert invite_ids == ["saas-pros/zt-slacktok-AAAA1234"], (
        f"expected only Slack lead, got {invite_ids}"
    )
