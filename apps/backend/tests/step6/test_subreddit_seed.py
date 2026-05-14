"""Subreddit cold-start seeding — pure helper tests.

The end-to-end seed_subreddits_for_campaign function hits live Reddit, so it
gets a smoke test only. The DEFAULT_SUBS / INDUSTRY_TO_SUBREDDITS resolution
and platform-anchor mapping are pure functions — those get exhaustive checks.
"""
import pytest

from src.db.models import Campaign
from src.pipeline.shared.subreddit_seed import (
    DEFAULT_SUBS,
    INDUSTRY_TO_SUBREDDITS,
    PLATFORM_ANCHORS,
    _resolve_platform_anchor,
    _resolve_subs_for_campaign,
)


# ---------- Platform anchor mapping ----------


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("whatsapp", "chat.whatsapp.com"),
        ("discord", "discord.gg"),
        ("slack", "join.slack.com"),
        ("WhatsApp", "chat.whatsapp.com"),  # case-insensitive
        ("SLACK", "join.slack.com"),
        # Unsupported / junk → None
        ("telegram", None),
        ("", None),
        (None, None),
    ],
)
def test_resolve_platform_anchor(platform, expected):
    assert _resolve_platform_anchor(platform) == expected


def test_platform_anchors_cover_all_supported_platforms():
    """Defensive: if a new platform is added to the campaign model, the seed
    module should also know about it. This test fails loudly if we forget."""
    expected_platforms = {"whatsapp", "discord", "slack"}
    assert set(PLATFORM_ANCHORS.keys()) == expected_platforms


# ---------- Industry → subreddits resolution ----------


def test_resolve_subs_exact_industry_match():
    c = Campaign(name="x", industries=["marketing"], platform="whatsapp")
    subs = _resolve_subs_for_campaign(c)
    assert "marketing" in [s.lower() for s in subs]
    assert "digital_marketing" in [s.lower() for s in subs]


def test_resolve_subs_case_insensitive():
    c = Campaign(name="x", industries=["Marketing"], platform="whatsapp")
    assert _resolve_subs_for_campaign(c) == INDUSTRY_TO_SUBREDDITS["marketing"]


def test_resolve_subs_substring_match():
    """Industry 'Marketing agency' should pick up the 'marketing' curated list
    via substring fallback. Real campaigns use multi-word industry labels."""
    c = Campaign(name="x", industries=["Marketing agency"], platform="whatsapp")
    subs = _resolve_subs_for_campaign(c)
    # Should resolve to either the marketing or agency curated list — both
    # contain "marketing" subs, so we just check we didn't fall to default.
    assert subs != DEFAULT_SUBS
    assert any("marketing" in s.lower() for s in subs)


def test_resolve_subs_unknown_industry_uses_default():
    c = Campaign(name="x", industries=["quantum_computing_xyz"], platform="whatsapp")
    assert _resolve_subs_for_campaign(c) == DEFAULT_SUBS


def test_resolve_subs_no_industry_uses_default():
    c = Campaign(name="x", industries=[], platform="whatsapp")
    assert _resolve_subs_for_campaign(c) == DEFAULT_SUBS

    c2 = Campaign(name="y", industries=None, platform="whatsapp")
    assert _resolve_subs_for_campaign(c2) == DEFAULT_SUBS


def test_resolve_subs_uses_first_industry_only():
    """If campaign has multiple industries, we only resolve from the first.
    This matches how query_builders.py treats the industry list."""
    c = Campaign(
        name="x",
        industries=["saas", "real estate"],
        platform="whatsapp",
    )
    subs = _resolve_subs_for_campaign(c)
    # Should match SaaS list, not real estate
    assert subs == INDUSTRY_TO_SUBREDDITS["saas"]


def test_resolve_subs_returns_a_copy():
    """Mutating the returned list must NOT corrupt the curated map. A
    downstream caller appending to the list (e.g. snowball-discovered subs)
    shouldn't leak into the next campaign's resolution."""
    c = Campaign(name="x", industries=["marketing"], platform="whatsapp")
    subs = _resolve_subs_for_campaign(c)
    original_len = len(INDUSTRY_TO_SUBREDDITS["marketing"])
    subs.append("definitely_not_a_real_sub_xyz")
    assert len(INDUSTRY_TO_SUBREDDITS["marketing"]) == original_len


# ---------- Defensive checks on the curated data ----------


def test_no_curated_list_is_empty():
    for industry, subs in INDUSTRY_TO_SUBREDDITS.items():
        assert subs, f"industry {industry!r} has an empty subreddit list"


def test_default_subs_is_nonempty():
    assert DEFAULT_SUBS, "DEFAULT_SUBS must have at least one fallback"


def test_no_curated_sub_has_r_prefix():
    """Sub names should be bare ('Entrepreneur'), not prefixed ('r/Entrepreneur'),
    so they pass straight to reddit.subreddit() without stripping."""
    all_subs = list(DEFAULT_SUBS)
    for subs in INDUSTRY_TO_SUBREDDITS.values():
        all_subs.extend(subs)
    bad = [s for s in all_subs if s.startswith(("r/", "/r/"))]
    assert not bad, f"these subs have an r/ prefix: {bad}"
