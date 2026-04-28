import os

import httpx
import pytest

# When run via `docker compose exec api pytest`, the API is on the same loopback.
# When run from another container on the compose network, override with API_URL=http://api:8000.
API_URL = os.getenv("API_URL", "http://localhost:8000")


@pytest.mark.asyncio
async def test_health_endpoint_returns_200_with_db_and_redis_ok():
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{API_URL}/health")
    assert resp.status_code == 200, f"unexpected status {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body == {"db": "ok", "redis": "ok"}, f"unexpected body: {body}"
