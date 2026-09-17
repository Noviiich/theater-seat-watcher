"""Access control for every Telegram update, including callback queries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class PrivateAllowlistMiddleware(BaseMiddleware):
    """Reject non-private or unapproved senders before a handler sees an update."""

    def __init__(self, allowed_user_ids: frozenset[str]) -> None:
        self._allowed_user_ids = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.type != "private":
            return None
        sender = event.from_user if isinstance(event, (CallbackQuery, Message)) else None
        if sender is None or str(sender.id) not in self._allowed_user_ids:
            return None
        return await handler(event, data)
