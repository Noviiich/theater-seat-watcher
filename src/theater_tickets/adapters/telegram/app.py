"""Composition helpers for a safely scoped aiogram dispatcher."""

from __future__ import annotations

from aiogram import Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.telegram.middleware import PrivateAllowlistMiddleware
from theater_tickets.adapters.telegram.routers.subscriptions import build_subscription_router


def build_dispatcher(
    *, allowed_user_ids: frozenset[str], session_factory: async_sessionmaker[AsyncSession]
) -> Dispatcher:
    """Build a dispatcher; starting long polling remains a later runtime step."""
    dispatcher = Dispatcher()
    gate = PrivateAllowlistMiddleware(allowed_user_ids)
    dispatcher.message.outer_middleware(gate)
    dispatcher.callback_query.outer_middleware(gate)
    dispatcher.include_router(build_subscription_router(session_factory))
    return dispatcher
