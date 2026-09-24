"""Access control for every Telegram update, including callback queries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class PrivateAccessMiddleware(BaseMiddleware):
    """Permit an administrator, approved users, and the narrow access-request flow."""

    def __init__(
        self,
        *,
        administrator_user_id: str,
        is_granted: Callable[[str], Awaitable[bool]],
    ) -> None:
        self._administrator_user_id = administrator_user_id
        self._is_granted = is_granted

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
        if sender is None:
            return None
        sender_id = str(sender.id)
        if sender_id == self._administrator_user_id or await self._is_granted(sender_id):
            return await handler(event, data)
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "access:request":
            return await handler(event, data)
        return None
