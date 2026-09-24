"""Composition helpers for a safely scoped aiogram dispatcher."""

from __future__ import annotations

from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.access import SqlAlchemyTelegramAccess
from theater_tickets.adapters.persistence.runtime import SqlAlchemyStatusReader
from theater_tickets.adapters.telegram.middleware import PrivateAccessMiddleware
from theater_tickets.adapters.telegram.routers.subscriptions import build_subscription_router
from theater_tickets.settings import BookingMode


def build_dispatcher(
    *,
    administrator_user_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    catalogue_stale_after_seconds: int = 180,
    booking_mode: BookingMode = BookingMode.DRY_RUN,
) -> Dispatcher:
    """Build an owner-scoped dispatcher for the runtime long-polling task."""
    dispatcher = Dispatcher()
    access = SqlAlchemyTelegramAccess(session_factory)
    gate = PrivateAccessMiddleware(
        administrator_user_id=administrator_user_id,
        is_granted=access.is_granted,
    )
    dispatcher.message.outer_middleware(gate)
    dispatcher.callback_query.outer_middleware(gate)
    dispatcher.include_router(
        build_subscription_router(
            session_factory,
            status_reader=SqlAlchemyStatusReader(session_factory),
            catalogue_stale_after_seconds=catalogue_stale_after_seconds,
            administrator_user_id=administrator_user_id,
            access=access,
            booking_mode=booking_mode,
        )
    )
    return dispatcher
