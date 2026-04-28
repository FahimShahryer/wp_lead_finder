import logging

from firecrawl import AsyncFirecrawl

from src.core.config import settings

logger = logging.getLogger(__name__)


class FirecrawlPermanentError(Exception):
    """Site-not-supported / paywall / 403 — don't retry. Reason in `.reason`."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_client: AsyncFirecrawl | None = None


def _get_client() -> AsyncFirecrawl:
    global _client
    if _client is None:
        if not settings.firecrawl_api_key:
            raise RuntimeError(
                "FIRECRAWL_API_KEY is not set. Add it to .env and recreate the api container."
            )
        _client = AsyncFirecrawl(api_key=settings.firecrawl_api_key)
    return _client


def _classify_error(exc: Exception) -> tuple[str, bool]:
    """Map a Firecrawl exception to (reason, is_permanent).
    is_permanent=True → mark fetch_failed and don't retry.
    is_permanent=False → bubble up so the caller can retry / count."""
    name = type(exc).__name__
    if name == "WebsiteNotSupportedError":
        return ("site_not_supported", True)
    if name == "PaymentRequiredError":
        return ("paywall", True)
    if name == "BadRequestError":
        return ("bad_request", True)
    if name == "UnauthorizedError":
        return ("unauthorized", True)
    if name == "RequestTimeoutError":
        return ("timeout", False)
    if name == "RateLimitError":
        return ("rate_limited", False)
    if name == "InternalServerError":
        return ("server_error", False)
    return (f"unexpected:{name}", True)  # default to permanent so we don't loop forever


async def scrape_markdown(url: str) -> str:
    """Scrape `url` and return its markdown. Raises FirecrawlPermanentError for
    permanent failures so the caller marks the row fetch_failed cleanly."""
    client = _get_client()
    try:
        doc = await client.scrape(url, formats=["markdown"])
    except Exception as e:
        reason, permanent = _classify_error(e)
        if permanent:
            raise FirecrawlPermanentError(reason)
        # transient — re-raise so caller's retry/budget logic sees it
        raise

    markdown = getattr(doc, "markdown", None) or ""
    if not markdown.strip():
        raise FirecrawlPermanentError("empty_markdown")
    return markdown
