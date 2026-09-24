from __future__ import annotations

import asyncio
from typing import Any

from aiogram.types import CallbackQuery, Message, TelegramObject

from theater_tickets.adapters.telegram.middleware import PrivateAccessMiddleware


def _message(*, user_id: int, chat_type: str = "private", text: str = "/subscriptions") -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": 0,
            "chat": {"id": user_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        }
    )


def test_private_access_allows_administrator_approved_users_and_access_requests() -> None:
    async def scenario() -> None:
        async def is_granted(user_id: str) -> bool:
            return user_id == "11"

        middleware = PrivateAccessMiddleware(
            administrator_user_id="10",
            is_granted=is_granted,
        )
        calls: list[TelegramObject] = []

        async def handler(event: TelegramObject, _: dict[str, Any]) -> str:
            calls.append(event)
            return "handled"

        assert await middleware(handler, _message(user_id=10), {}) == "handled"
        assert await middleware(handler, _message(user_id=11), {}) == "handled"
        assert await middleware(handler, _message(user_id=10, chat_type="group"), {}) is None
        start = _message(user_id=12, text="/start")
        assert await middleware(handler, start, {}) == "handled"

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
        denied_callback = CallbackQuery.model_validate(
            {
                "id": "denied-callback-id",
                "from": {"id": 12, "is_bot": False, "first_name": "Test"},
                "chat_instance": "instance",
                "data": "stop:candidate",
                "message": _message(user_id=12).model_dump(mode="json", by_alias=True),
            }
        )
        assert await middleware(handler, denied_callback, {}) is None
        assert calls == [_message(user_id=10), _message(user_id=11), start, callback]

    asyncio.run(scenario())
