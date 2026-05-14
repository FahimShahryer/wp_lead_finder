"""Tests for CreateCampaignRequest.industries validation.

The form locks input to exactly one industry, 1-2 words, letters and single
spaces only — no commas, digits, slashes, hyphens. This validator enforces
that contract at the API boundary so a hand-rolled curl can't bypass the
front-end's filtering.
"""
import pytest
from pydantic import ValidationError

from src.main import CreateCampaignRequest


def _make(**overrides):
    """Build a minimal valid request with overrides."""
    base = {
        "name": "smoke",
        "industries": ["Marketing"],
        "platform": "whatsapp",
    }
    base.update(overrides)
    return CreateCampaignRequest(**base)


# ---------- Accepted shapes ----------


@pytest.mark.parametrize(
    "industry",
    [
        "Marketing",            # single word
        "marketing",            # lowercase
        "MARKETING",            # uppercase
        "Marketing Agency",     # two words
        "SaaS",                 # mixed case acronym
        "Real Estate",          # two words
        "AI",                   # short
        "Healthcare",           # longer single word
    ],
)
def test_valid_industries_pass(industry):
    req = _make(industries=[industry])
    assert req.industries == [industry.strip()]


def test_whitespace_is_normalized():
    """Leading/trailing/extra-internal spaces get collapsed to a single space."""
    req = _make(industries=["  Marketing   Agency  "])
    assert req.industries == ["Marketing Agency"]


def test_industries_is_stored_as_single_element_list():
    """DB shape is still ARRAY(Text) — we just enforce length=1 at the API."""
    req = _make(industries=["Marketing"])
    assert isinstance(req.industries, list)
    assert len(req.industries) == 1


# ---------- Rejected shapes ----------


@pytest.mark.parametrize(
    "industries,reason",
    [
        ([], "empty list"),
        (["Marketing", "SaaS"], "two industries"),
        ([""], "blank string"),
        (["   "], "whitespace-only"),
        (["Marketing Agency Owners"], "three words"),
        (["AI agency owners"], "three words"),
        (["Marketing, SaaS"], "contains comma"),
        (["Marketing/SaaS"], "contains slash"),
        (["B2B"], "contains digit"),
        (["Marketing & sales"], "contains ampersand"),
        (["Marketing-Agency"], "contains hyphen"),
        (["Marketing.Agency"], "contains period"),
        (["café"], "non-ASCII letter"),
        (["💼 Marketing"], "emoji"),
        (["agency owners (US)"], "parens + extra word"),
    ],
)
def test_invalid_industries_rejected(industries, reason):
    with pytest.raises(ValidationError) as exc_info:
        _make(industries=industries)
    # The error message should mention "industries" so an API caller can
    # tell which field tripped them.
    assert "industries" in str(exc_info.value).lower(), reason
