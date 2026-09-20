from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from theater_tickets.adapters.telegram.notifier import AiogramNotificationTransport
from theater_tickets.application.outbox import (
    OutboxItem,
    OutboxKind,
    batch_summary_message,
    payment_message,
)


def test_payment_text_contains_details_but_never_the_payment_url() -> None:
    starts_at = datetime(2026, 10, 24, 13, tzinfo=UTC)
    expires_at = datetime(2026, 10, 24, 14, tzinfo=UTC)
    secret_url = "https://quicktickets.ru/payment/order/secret-token"
    text = payment_message(
        order_id="local-order",
        cycle_no=2,
        title="Спектакль",
        starts_at=starts_at,
        seat_ids=("Ряд 1 / 12", "Ряд 1 / 13"),
        total_minor=140_050,
        currency="RUB",
        expires_at=expires_at,
    )
    item = OutboxItem(
        outbox_id="outbox",
        kind=OutboxKind.PAYMENT,
        destination_chat_id="10",
        destination_user_id="10",
        text=text,
        attempts=1,
        payment_url=secret_url,
        stop_callback_data="stop:candidate",
    )

    assert "Спектакль" in text
    assert "Ряд 1 / 12, Ряд 1 / 13" in text
    assert "1 400,50 ₽" in text
    assert "цикл №2" in text
    assert secret_url not in text
    assert secret_url not in repr(item)


def test_batch_summary_renders_each_session_status() -> None:
    from theater_tickets.application.outbox import BatchSessionResult

    text = batch_summary_message(
        (
            BatchSessionResult("Сеанс A", "ссылка отправлена"),
            BatchSessionResult("Сеанс B", "нет соседних мест"),
        )
    )

    assert "Сеанс A: ссылка отправлена" in text
    assert "Сеанс B: нет соседних мест" in text


def test_aiogram_transport_uses_buttons_and_disables_link_preview() -> None:
    class FakeBot:
        send_kwargs: dict[str, object] | None = None
        edit_kwargs: dict[str, object] | None = None

        async def send_message(self, **kwargs: object) -> object:
            self.send_kwargs = kwargs
            return SimpleNamespace(message_id=42)

        async def edit_message_text(self, **kwargs: object) -> None:
            self.edit_kwargs = kwargs

    async def scenario() -> None:
        bot = FakeBot()
        transport = AiogramNotificationTransport(bot)  # type: ignore[arg-type]
        item = OutboxItem(
            outbox_id="outbox",
            kind=OutboxKind.PAYMENT,
            destination_chat_id="10",
            destination_user_id="10",
            text="Без секретной ссылки в тексте",
            attempts=1,
            payment_url="https://quicktickets.ru/payment/order/secret",
            stop_callback_data="stop:candidate",
        )
        assert await transport.send(item) == "42"
        assert bot.send_kwargs is not None
        markup = bot.send_kwargs["reply_markup"]
        assert markup is not None
        assert markup.inline_keyboard[0][0].text == "Оплатить"  # type: ignore[union-attr]
        assert markup.inline_keyboard[1][0].callback_data == "stop:candidate"  # type: ignore[union-attr]
        preview = bot.send_kwargs["link_preview_options"]
        assert preview.is_disabled is True  # type: ignore[union-attr]

        await transport.expire(chat_id="10", message_id="42")
        assert bot.edit_kwargs is not None
        assert bot.edit_kwargs["reply_markup"] is None

    asyncio.run(scenario())
