import os

import httpx
import pytest

# Default to the compose-network hostname so this works from any container
# (worker, api, etc.). Inside the api container `http://api:8000` resolves to
# the same place as `http://localhost:8000`, so this default works everywhere.
# Override with API_URL=... when running outside compose.
API_URL = os.getenv("API_URL", "http://api:8000")


@pytest.mark.asyncio
async def test_health_endpoint_returns_200_with_db_and_redis_ok():
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{API_URL}/health")
    assert resp.status_code == 200, f"unexpected status {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body == {"db": "ok", "redis": "ok"}, f"unexpected body: {body}"
