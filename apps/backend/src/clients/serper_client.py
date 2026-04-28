import asyncio
import logging
from dataclasses import dataclass

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

SERPER_URL = "https://google.serper.dev/search"
DEFAULT_NUM_RESULTS = 10
MAX_RETRIES = 5


@dataclass
class SerperResult:
    url: str
    title: str | None
    snippet: str | None
    position: int | None


class SerperError(Exception):
    pass


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        if not settings.serper_api_key:
            raise RuntimeError(
                "SERPER_API_KEY is not set. Add it to .env and recreate the api container."
            )
        _client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "X-API-KEY": settings.serper_api_key,
                "Content-Type": "application/json",
            },
        )
    return _client


async def search(query: str, num: int = DEFAULT_NUM_RESULTS) -> list[SerperResult]:
    """Run a single Serper search. Retries on 429 (honoring Retry-After) and 5xx
    with exponential backoff. Raises SerperError after MAX_RETRIES."""
    client = _get_client()
    payload = {"q": query, "num": num}

    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.post(SERPER_URL, json=payload)
        except httpx.RequestError as e:
            wait = min(2**attempt, 30)
            logger.warning("serper transport error (attempt %d): %s; sleeping %ds", attempt, e, wait)
            await asyncio.sleep(wait)
            continue

        if resp.status_code == 200:
            return _parse(resp.json())

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", min(2**attempt, 30)))
            logger.warning("serper 429, retry-after %ss (attempt %d)", retry_after, attempt)
            await asyncio.sleep(retry_after)
            continue

        if 500 <= resp.status_code < 600:
            wait = min(2**attempt, 30)
            logger.warning("serper %d (attempt %d), sleeping %ds", resp.status_code, attempt, wait)
            await asyncio.sleep(wait)
            continue

        # 4xx other than 429 — permanent
        raise SerperError(f"serper non-retryable {resp.status_code}: {resp.text[:200]}")

    raise SerperError(f"serper exhausted {MAX_RETRIES} retries for query: {query[:80]!r}")


def _parse(body: dict) -> list[SerperResult]:
    organic = body.get("organic") or []
    results: list[SerperResult] = []
    for item in organic:
        link = item.get("link")
        if not link:
            continue
        results.append(
            SerperResult(
                url=link,
                title=item.get("title"),
                snippet=item.get("snippet"),
                position=item.get("position"),
            )
        )
    return results


async def aclose() -> None:
    """Close the global client. Optional; ok to leak in short-lived processes."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
