from __future__ import annotations

import asyncio
from typing import Any

from aiogram.types import CallbackQuery, Message, TelegramObject

from theater_tickets.adapters.telegram.middleware import PrivateAllowlistMiddleware


def _message(*, user_id: int, chat_type: str = "private") -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": user_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": "/subscriptions",
        }
    )


def test_private_allowlist_gates_messages_and_callback_queries() -> None:
    async def scenario() -> None:
        middleware = PrivateAllowlistMiddleware(frozenset({"10"}))
        calls: list[TelegramObject] = []

        async def handler(event: TelegramObject, _: dict[str, Any]) -> str:
            calls.append(event)
            return "handled"

        assert await middleware(handler, _message(user_id=10), {}) == "handled"
        assert await middleware(handler, _message(user_id=11), {}) is None
        assert await middleware(handler, _message(user_id=10, chat_type="group"), {}) is None

        callback = CallbackQuery.model_validate(
            {
                "id": "callback-id",
                "from": {"id": 10, "is_bot": False, "first_name": "Test"},
                "chat_instance": "instance",
                "data": "stop:candidate",
                "message": _message(user_id=10).model_dump(mode="json", by_alias=True),
            }
        )
        assert await middleware(handler, callback, {}) == "handled"
        assert calls == [_message(user_id=10), callback]

    asyncio.run(scenario())
