import pytest
from sqlalchemy import select

from src.db.models import Campaign, Query, SearchResult
from src.db.session import SessionLocal
from src.pipeline.stage3_prefilter import classify, prefilter_search_results

# ---------- Pure classifier fixtures ----------

CASES = [
    # 1: reddit URL with invite leaked into the snippet — must still classify as
    # 'reddit' so we fetch the full thread (comments typically hold many more
    # invites than the one Google surfaced in the snippet).
    (
        "https://reddit.com/r/agency/post1",
        "AI agency owners share invites",
        "join our group: chat.whatsapp.com/AbCdEf123 — link inside",
        "reddit",
    ),
    (
        "https://example.com/blog/123",
        "Found new chat.whatsapp.com/XyZpQr12 link",
        "summary text",
        "snippet_hit",
    ),
    (
        "https://chat.whatsapp.com/Abc1234567",
        "WhatsApp invite",
        None,
        "snippet_hit",
    ),

    # 4-7: reddit variants
    ("https://reddit.com/r/saas", "x", "y", "reddit"),
    ("https://www.reddit.com/r/saas/comments/abc", "x", "y", "reddit"),
    ("https://old.reddit.com/r/x", "x", "y", "reddit"),
    ("https://np.reddit.com/r/x", "x", "y", "reddit"),

    # 8-14: skip — known-blocked hosts
    ("https://youtube.com/watch?v=abc", "x", "y", "skip"),
    ("https://www.youtube.com/watch?v=abc", "x", "y", "skip"),
    ("https://youtu.be/abc", "x", "y", "skip"),
    ("https://m.facebook.com/groups/123", "x", "y", "skip"),
    ("https://www.instagram.com/handle", "x", "y", "skip"),
    ("https://linkedin.com/in/handle", "x", "y", "skip"),
    ("https://twitter.com/handle/status/1", "x", "y", "skip"),
    ("https://x.com/handle/status/1", "x", "y", "skip"),

    # 15-17: skip — file extensions
    ("https://example.com/doc/file.pdf", "x", "y", "skip"),
    ("https://example.com/img/photo.JPG", "x", "y", "skip"),
    ("https://example.com/audio/clip.mp3", "x", "y", "skip"),

    # 18: case-insensitive host
    ("https://REDDIT.COM/r/saas", "x", "y", "reddit"),

    # 19: case-insensitive snippet match
    (
        "https://example.com/post",
        None,
        "found CHAT.WHATSAPP.COM/Abc123 inside",
        "snippet_hit",
    ),

    # 20: tricky non-reddit (not a reddit subdomain, just contains 'reddit')
    ("https://foo-reddit.com/something", "x", "y", "web"),

    # 21-22: vanilla web
    ("https://medium.com/@user/article-123", "x", "y", "web"),
    (
        "https://forum.indiehackers.com/post/12",
        "Indie hackers community",
        "join us",
        "web",
    ),

    # 23-30: WhatsApp directory aggregators must be skipped EVEN when the
    # snippet contains an invite. These sites SEO-target our query shape and
    # would otherwise pollute the lead lifecycle with auto-scraped junk.
    (
        "https://newwhatsgroups.com/marketing-groups",
        "Top 50 marketing WhatsApp groups",
        "join chat.whatsapp.com/AbcDef1234 here",
        "skip",
    ),
    (
        "https://whtspgrouplink.com/agency",
        "Agency owners groups",
        "see chat.whatsapp.com/XyzPqr5678 below",
        "skip",
    ),
    (
        "https://www.thewhatsgrouplink.com/2026/saas-groups",
        "SaaS WhatsApp groups",
        "no invite in snippet",
        "skip",
    ),
    # Subdomain match
    (
        "https://m.joinchatgroups.com/cricket",
        "x",
        "y",
        "skip",
    ),
    # Heavy listicle aggregator
    ("https://whatsgroulinks.com/agency-owners", "x", "y", "skip"),
    ("https://wagroupslink.com/marketing", "x", "y", "skip"),
    ("https://wa-filter.com/finder", "x", "y", "skip"),
    # Lookalike that's NOT in the blocklist — must classify as web, not skip.
    # Defensive: confirms the blocklist is curated (not regex-pattern-based).
    ("https://whatsapp-business.com/articles/foo", "x", "y", "web"),
]


@pytest.mark.parametrize("url,title,snippet,expected", CASES)
def test_classify(url, title, snippet, expected):
    assert classify(url, title, snippet) == expected, (
        f"url={url!r} title={title!r} snippet={snippet!r}"
    )


# ---------- Integration: real DB rows get tagged ----------

def test_directory_blocklist_skips_even_when_invite_in_snippet():
    """The directory check fires BEFORE the snippet_hit check on purpose:
    these sites usually leak the invite into the snippet but the lead would
    be auto-scraped junk. Snippet-hit should NOT win over directory-skip."""
    from src.pipeline.stage3_prefilter import classify

    # All these have a clear invite in the snippet — would normally be
    # snippet_hit. Must still classify as skip.
    cases = [
        ("https://newwhatsgroups.com/x", "join chat.whatsapp.com/AbcDef1234 now"),
        ("https://whatgroups.com/y", "see chat.whatsapp.com/XyzPqr5678 below"),
        ("https://m.joinchatgroups.com/z", "chat.whatsapp.com/Hij1234567 link"),
    ]
    for url, snippet in cases:
        assert classify(url, None, snippet) == "skip", url


async def test_prefilter_tags_all_pending_rows_and_distribution_is_sane():
    async with SessionLocal() as s:
        c = Campaign(name="step5-integration", industries=["x"])
        s.add(c)
        await s.flush()
        q = Query(campaign_id=c.id, query_text="probe", source_platform="reddit", status="searched")
        s.add(q)
        await s.flush()

        # Seed a representative spread.
        seeds = [
            ("https://reddit.com/r/agency/post", "AI agency", "snippet without invite"),
            ("https://www.reddit.com/r/saas/comments/abc", "SaaS founders", "long discussion"),
            ("https://old.reddit.com/r/x", None, None),
            ("https://medium.com/@x/article", None, None),
            ("https://example.com/post", None, "join chat.whatsapp.com/Abc123 here"),
            ("https://chat.whatsapp.com/DirectInvite42", None, None),
            ("https://youtube.com/watch?v=x", None, None),
            ("https://linkedin.com/in/x", None, None),
            ("https://example.com/file.pdf", None, None),
            ("https://forum.indiehackers.com/post", None, None),
        ]
        for url, title, snippet in seeds:
            s.add(
                SearchResult(
                    query_id=q.id, url=url, title=title, snippet=snippet, status="new"
                )
            )
        await s.commit()
        cid = c.id

    async with SessionLocal() as s:
        counts = await prefilter_search_results(s, cid)

    # Expected distribution from the seed:
    # snippet_hit: 2 (chat.whatsapp.com URL + snippet match)
    # reddit:     3 (reddit.com, www.reddit.com, old.reddit.com)
    # skip:       3 (youtube, linkedin, .pdf)
    # web:        2 (medium.com, forum.indiehackers.com)
    assert counts == {"snippet_hit": 2, "reddit": 3, "web": 2, "skip": 3}, counts

    # Zero-unrouted gate
    async with SessionLocal() as s:
        unrouted = (
            await s.execute(
                select(SearchResult)
                .join(Query)
                .where(Query.campaign_id == cid, SearchResult.fetch_strategy.is_(None))
            )
        ).scalars().all()
    assert unrouted == [], f"unrouted rows after stage 3: {[r.url for r in unrouted]}"


async def test_prefilter_is_idempotent():
    async with SessionLocal() as s:
        c = Campaign(name="step5-idempotent", industries=["x"])
        s.add(c)
        await s.flush()
        q = Query(campaign_id=c.id, query_text="probe", status="searched")
        s.add(q)
        await s.flush()
        s.add(SearchResult(query_id=q.id, url="https://reddit.com/r/x", status="new"))
        await s.commit()
        cid = c.id

    async with SessionLocal() as s:
        first = await prefilter_search_results(s, cid)
    async with SessionLocal() as s:
        second = await prefilter_search_results(s, cid)

    assert first == {"snippet_hit": 0, "reddit": 1, "web": 0, "skip": 0}
    assert second == {"snippet_hit": 0, "reddit": 0, "web": 0, "skip": 0}, (
        "second run should re-tag zero rows"
    )
