"""Aiogram transport for already-rendered persistent outbox messages."""

from __future__ import annotations

import asyncio
from math import ceil
from time import monotonic

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from theater_tickets.application.outbox import (
    NotificationRateLimited,
    OutboxItem,
    expired_message,
)


class AiogramNotificationTransport:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._next_send_at: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}

    async def send(self, item: OutboxItem) -> str:
        chat = item.destination_chat_id
        blocked = self._blocked_until.get(chat, 0) - monotonic()
        if blocked > 0:
            raise NotificationRateLimited(max(1, ceil(blocked)))
        delay = self._next_send_at.get(chat, 0) - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_send_at[chat] = monotonic() + 1.0
        buttons: list[list[InlineKeyboardButton]] = []
        if item.payment_url is not None:
            buttons.append([InlineKeyboardButton(text="Оплатить", url=item.payment_url)])
        if item.stop_callback_data is not None:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Остановить повторы",
                        callback_data=item.stop_callback_data,
                    )
                ]
            )
        markup = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        try:
            message = await self._bot.send_message(
                chat_id=item.destination_chat_id,
                text=item.text,
                reply_markup=markup,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except TelegramRetryAfter as exc:
            self._blocked_until[chat] = monotonic() + exc.retry_after
            raise NotificationRateLimited(int(exc.retry_after)) from exc
        return str(message.message_id)

    async def expire(self, *, chat_id: str, message_id: str) -> None:
        await self._bot.edit_message_text(
            chat_id=chat_id,
            message_id=int(message_id),
            text=expired_message(),
            reply_markup=None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
