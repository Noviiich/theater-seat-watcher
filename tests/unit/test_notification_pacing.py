from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from theater_tickets.adapters.telegram.notifier import AiogramNotificationTransport
from theater_tickets.application.outbox import NotificationRateLimited, OutboxItem, OutboxKind


def test_provider_retry_after_defers_other_links_for_the_same_chat() -> None:
    async def scenario() -> None:
        retry = TelegramRetryAfter(
            method=SendMessage(chat_id=1, text="test"), message="retry", retry_after=30
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(side_effect=[retry, SimpleNamespace(message_id=2)])
        )
        transport = AiogramNotificationTransport(bot)
        item = OutboxItem("first", OutboxKind.PAYMENT, "1", "1", "test", 0)
        with pytest.raises(NotificationRateLimited):
            await transport.send(item)
        with pytest.raises(NotificationRateLimited):
            await transport.send(item)
        assert bot.send_message.await_count == 1
        other = OutboxItem("other", OutboxKind.PAYMENT, "2", "2", "test", 0)
        assert await transport.send(other) == "2"

    asyncio.run(scenario())
