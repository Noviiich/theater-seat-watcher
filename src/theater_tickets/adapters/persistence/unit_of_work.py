"""Short database transaction boundary for application use cases."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class SqlAlchemyUnitOfWork:
    """Commits on success and rolls back every exception."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        self.session = self._session_factory()
        await self.session.begin()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        assert self.session is not None
        try:
            if exc_type is None:
                await self.session.commit()
            else:
                await self.session.rollback()
        except BaseException:
            await self.session.rollback()
            raise
        finally:
            await self.session.close()
