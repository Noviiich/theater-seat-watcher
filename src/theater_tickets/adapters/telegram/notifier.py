"""Aiogram transport for already-rendered persistent outbox messages."""

from __future__ import annotations

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

    async def send(self, item: OutboxItem) -> str:
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
