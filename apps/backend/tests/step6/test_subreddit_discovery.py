"""Subreddit-direct discovery tests.

Pure unit tests for URL parsing and productive-sub identification (no Reddit
API needed). The end-to-end discover_via_productive_subreddits function
hits live Reddit; we don't test it directly here, but the conftest gating
in step6/test_reddit_fetch.py applies if you want to add a smoke test later.
"""
import pytest

from src.db.models import Campaign, Lead
from src.db.session import SessionLocal
from src.pipeline.shared.subreddit_discovery import (
    _build_search_query,
    _extract_subreddit_from_url,
    _productive_subs,
)


# ---------- _extract_subreddit_from_url ----------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://reddit.com/r/Entrepreneur/comments/abc/title/", "entrepreneur"),
        ("https://www.reddit.com/r/saas", "saas"),
        ("https://old.reddit.com/r/marketing/comments/xyz", "marketing"),
        ("https://np.reddit.com/r/AIagencies/", "aiagencies"),
        ("https://m.reddit.com/r/Entrepreneur", "entrepreneur"),
        # Non-reddit hosts → None
        ("https://medium.com/@user/article", None),
        ("https://meetup.com/groups/123", None),
        # Reddit but not a sub URL (e.g. user profile) → None
        ("https://reddit.com/user/foo", None),
        ("https://reddit.com/", None),
        # Junk inputs → None
        ("", None),
        ("not a url", None),
    ],
)
def test_extract_subreddit_from_url(url, expected):
    assert _extract_subreddit_from_url(url) == expected


# ---------- _build_search_query ----------


def test_build_search_query_uses_first_industry_term():
    c = Campaign(name="x", industries=["Marketing agency", "AI agency"])
    assert _build_search_query(c) == "Marketing agency whatsapp"


def test_build_search_query_falls_back_when_no_industry():
    c = Campaign(name="x", industries=[])
    assert _build_search_query(c) == "whatsapp invite"
    c2 = Campaign(name="y", industries=None)
    assert _build_search_query(c2) == "whatsapp invite"


# ---------- _productive_subs (DB) ----------


async def _seed_campaign_with_lead_urls(name: str, urls: list[str]) -> int:
    async with SessionLocal() as s:
        c = Campaign(name=name, industries=["x"])
        s.add(c)
        await s.flush()
        for i, url in enumerate(urls):
            s.add(
                Lead(
                    campaign_id=c.id,
                    invite_id=f"sub_disco_{i}",
                    source_url=url,
                )
            )
        await s.commit()
        return c.id


async def test_productive_subs_counts_distinct_subreddits():
    cid = await _seed_campaign_with_lead_urls(
        "step6-disco-counts",
        urls=[
            "https://reddit.com/r/Entrepreneur/post1",
            "https://reddit.com/r/Entrepreneur/post2",
            "https://reddit.com/r/saas/post3",
            "https://medium.com/@user/article",  # non-reddit, ignored
        ],
    )
    subs = await _productive_subs(cid)
    by_name = dict(subs)
    assert by_name == {"entrepreneur": 2, "saas": 1}
    # Sorted by count desc — entrepreneur first.
    assert subs[0][0] == "entrepreneur"


async def test_productive_subs_ignores_campaigns_with_no_reddit_leads():
    cid = await _seed_campaign_with_lead_urls(
        "step6-disco-empty",
        urls=[
            "https://medium.com/@user/article",
            "https://meetup.com/groups/foo",
        ],
    )
    subs = await _productive_subs(cid)
    assert subs == []


async def test_productive_subs_respects_max_per_pass():
    """When N subs each have ≥1 lead, we still cap the returned list at
    MAX_SUBS_PER_PASS. Only the top-N by lead count come back."""
    from src.pipeline.shared.subreddit_discovery import MAX_SUBS_PER_PASS

    # Build a campaign with leads in MAX+5 distinct subs, with different
    # counts so ordering is unambiguous.
    urls: list[str] = []
    n_extra = 5
    for i in range(MAX_SUBS_PER_PASS + n_extra):
        # Sub with index `i` gets `i+1` leads → higher-index subs have more
        # leads → top of the list after sorting.
        for _ in range(i + 1):
            urls.append(f"https://reddit.com/r/sub{i}/post{i}-{_}")

    cid = await _seed_campaign_with_lead_urls("step6-disco-cap", urls=urls)
    subs = await _productive_subs(cid)
    assert len(subs) == MAX_SUBS_PER_PASS
    # The very last sub (index MAX+n_extra-1) had the most leads → first.
    assert subs[0][0] == f"sub{MAX_SUBS_PER_PASS + n_extra - 1}"
