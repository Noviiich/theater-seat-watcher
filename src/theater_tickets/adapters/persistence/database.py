"""Async SQLite engine and session factory."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_session_factory(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create an engine without opening a connection or running migrations."""
    engine = create_async_engine(database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)
