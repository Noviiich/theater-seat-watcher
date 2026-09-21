"""Composition helpers for a safely scoped aiogram dispatcher."""

from __future__ import annotations

from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.runtime import SqlAlchemyStatusReader
from theater_tickets.adapters.telegram.middleware import PrivateAllowlistMiddleware
from theater_tickets.adapters.telegram.routers.subscriptions import build_subscription_router


def build_dispatcher(
    *,
    allowed_user_ids: frozenset[str],
    session_factory: async_sessionmaker[AsyncSession],
    catalogue_stale_after_seconds: int = 180,
) -> Dispatcher:
    """Build an owner-scoped dispatcher for the runtime long-polling task."""
    dispatcher = Dispatcher()
    gate = PrivateAllowlistMiddleware(allowed_user_ids)
    dispatcher.message.outer_middleware(gate)
    dispatcher.callback_query.outer_middleware(gate)
    dispatcher.include_router(
        build_subscription_router(
            session_factory,
            status_reader=SqlAlchemyStatusReader(session_factory),
            catalogue_stale_after_seconds=catalogue_stale_after_seconds,
        )
    )
    return dispatcher
