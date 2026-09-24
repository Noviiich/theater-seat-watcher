"""Persistent administrator decisions for Telegram access."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.models import TelegramAccessModel


class SqlAlchemyTelegramAccess:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def is_granted(self, telegram_user_id: str) -> bool:
        async with self._session_factory() as session:
            state = await session.scalar(
                select(TelegramAccessModel.state).where(
                    TelegramAccessModel.telegram_user_id == telegram_user_id
                )
            )
        return state == "granted"

    async def request(self, *, telegram_user_id: str, telegram_chat_id: str) -> bool:
        """Create or refresh a request. Return True only when admin notification is due."""
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            request = await session.get(TelegramAccessModel, telegram_user_id)
            if request is not None and request.state in {"pending", "granted"}:
                request.telegram_chat_id = telegram_chat_id
                return False
            if request is None:
                session.add(
                    TelegramAccessModel(
                        telegram_user_id=telegram_user_id,
                        telegram_chat_id=telegram_chat_id,
                        state="pending",
                        requested_at=now,
                        decided_at=None,
                    )
                )
            else:
                request.telegram_chat_id = telegram_chat_id
                request.state = "pending"
                request.requested_at = now
                request.decided_at = None
        return True

    async def decide(self, *, telegram_user_id: str, granted: bool) -> str | None:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            request = await session.get(TelegramAccessModel, telegram_user_id)
            if request is None or request.state != "pending":
                return None
            request.state = "granted" if granted else "rejected"
            request.decided_at = now
            return request.telegram_chat_id
