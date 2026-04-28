import pytest
from sqlalchemy import select

from src.db.models import Campaign, Query, SearchResult
from src.db.session import SessionLocal
from src.pipeline.stage3_prefilter import classify, prefilter_search_results

# ---------- Pure classifier fixtures ----------

CASES = [
    # 1-3: snippet_hit (invite link visible)
    (
        "https://reddit.com/r/agency/post1",
        "AI agency owners share invites",
        "join our group: chat.whatsapp.com/AbCdEf123 — link inside",
        "snippet_hit",
    ),
    (
        "https://example.com/blog/123",
        "Found new chat.whatsapp.com/XyZ link",
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
]


@pytest.mark.parametrize("url,title,snippet,expected", CASES)
def test_classify(url, title, snippet, expected):
    assert classify(url, title, snippet) == expected, (
        f"url={url!r} title={title!r} snippet={snippet!r}"
    )


# ---------- Integration: real DB rows get tagged ----------

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
