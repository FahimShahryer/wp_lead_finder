import pytest

from src.db.session import SessionLocal


@pytest.fixture
async def session():
    """Yields a fresh AsyncSession; rolls back any uncommitted state on teardown.
    The db-wipe fixture in tests/conftest.py handles cleanup between tests."""
    async with SessionLocal() as s:
        yield s
        await s.rollback()
