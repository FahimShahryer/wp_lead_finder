from openai import AsyncOpenAI

from src.core.config import settings

# Default model for query generation + scoring. The architecture pegs gpt-4o-mini.
QUERY_GEN_MODEL = "gpt-4o-mini"

_client: AsyncOpenAI | None = None


def get_openai_client() -> AsyncOpenAI:
    """Lazy singleton. SDK does exponential backoff on 429/5xx internally
    (max_retries), and we cap a single request at 60s."""
    global _client
    if _client is None:
        if not settings.openai_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env and recreate the api container."
            )
        _client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            timeout=60.0,
            max_retries=5,
        )
    return _client
