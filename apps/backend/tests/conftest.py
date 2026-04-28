import pytest
from sqlalchemy import delete

from src.db.models import Campaign, UrlCache
from src.db.session import SessionLocal


@pytest.fixture(autouse=True)
async def _isolate_db():
    """After every test: wipe campaigns + url_cache so cases don't leak.
    Cascade FKs handle queries / search_results / leads."""
    yield
    async with SessionLocal() as s:
        await s.execute(delete(Campaign))
        await s.execute(delete(UrlCache))
        await s.commit()
