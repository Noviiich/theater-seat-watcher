"""Subscription commands and owner-scoped callbacks."""

from __future__ import annotations

from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.repositories import SubscriptionRepository
from theater_tickets.domain.models import BookingMode, Money, Subscription

THEATRE_ALIAS = "orel-teatr-svobodnoe-prostranstvo"
DEFAULT_MAX_SESSIONS_PER_BATCH = 1


def _help() -> str:
    return (
        "Команды: /subscribe <количество> <профиль> [максимум_рублей], "
        "/subscriptions, /pause <id>, /resume <id>, /stop <candidate-id>, /status.\n"
        "Новая подписка создаётся в dry_run: live включается только после проверки профиля зала."
    )


def _summary(items: tuple[Subscription, ...]) -> str:
    if not items:
        return "Подписок пока нет. Используйте /subscribe <количество> <профиль>."
    return "\n".join(
        f"{item.subscription_id}: {'включена' if item.enabled else 'пауза'}; "
        f"{item.ticket_count} билет(а); профиль {item.seat_profile_id}; {item.booking_mode.value}"
        for item in items
    )


def _new_subscription(command: CommandObject, user_id: str) -> Subscription | str:
    parts = (command.args or "").split()
    if len(parts) not in (2, 3):
        return "Формат: /subscribe <количество> <профиль> [максимум_рублей]."
    try:
        count = int(parts[0])
        max_total = Money.from_rubles(parts[2]) if len(parts) == 3 else None
    except ValueError:
        return "Количество должно быть целым, а сумма — точным числом рублей."
    return Subscription(
        subscription_id=str(uuid4()),
        buyer_id=user_id,
        theatre_alias=THEATRE_ALIAS,
        ticket_count=count,
        seat_profile_id=parts[1],
        max_sessions_per_batch=DEFAULT_MAX_SESSIONS_PER_BATCH,
        max_order_total=max_total,
        booking_mode=BookingMode.DRY_RUN,
    )


def build_subscription_router(session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router(name="subscriptions")

    @router.message(Command("start", "help"))
    async def help_command(message: Message) -> None:
        await message.answer(_help())

    @router.message(Command("subscribe"))
    async def subscribe(message: Message, command: CommandObject) -> None:
        assert message.from_user is not None
        value = _new_subscription(command, str(message.from_user.id))
        if isinstance(value, str):
            await message.answer(value)
            return
        async with session_factory() as session, session.begin():
            await SubscriptionRepository(session).add(value, telegram_chat_id=str(message.chat.id))
        await message.answer(
            f"Подписка сохранена: {value.subscription_id}.\n"
            f"{value.ticket_count} билет(а), профиль {value.seat_profile_id}, режим dry_run."
        )

    @router.message(Command("subscriptions", "settings", "status", "preview", "orders"))
    async def list_subscriptions(message: Message) -> None:
        assert message.from_user is not None
        async with session_factory() as session:
            items = await SubscriptionRepository(session).list_for_telegram_user(
                str(message.from_user.id)
            )
        await message.answer(_summary(items))

    @router.message(Command("pause", "resume"))
    async def set_subscription(message: Message, command: CommandObject) -> None:
        assert message.from_user is not None
        subscription_id = (command.args or "").strip()
        if not subscription_id:
            await message.answer(f"Формат: /{command.command} <id подписки>.")
            return
        enabled = command.command == "resume"
        async with session_factory() as session, session.begin():
            changed = await SubscriptionRepository(session).set_enabled(
                subscription_id=subscription_id,
                telegram_user_id=str(message.from_user.id),
                enabled=enabled,
            )
        await message.answer("Настройка изменена." if changed else "Подписка не найдена.")

    @router.message(Command("stop"))
    async def stop(message: Message, command: CommandObject) -> None:
        assert message.from_user is not None
        candidate_id = (command.args or "").strip()
        if not candidate_id:
            await message.answer("Формат: /stop <id сеанса-кандидата>.")
            return
        async with session_factory() as session, session.begin():
            changed = await SubscriptionRepository(session).stop_candidate(
                candidate_id=candidate_id, telegram_user_id=str(message.from_user.id)
            )
        await message.answer("Повторы остановлены." if changed else "Сеанс не найден.")

    @router.callback_query(F.data.startswith("stop:"))
    async def stop_callback(callback: CallbackQuery) -> None:
        assert callback.from_user is not None
        candidate_id = (callback.data or "").removeprefix("stop:")
        async with session_factory() as session, session.begin():
            changed = await SubscriptionRepository(session).stop_candidate(
                candidate_id=candidate_id, telegram_user_id=str(callback.from_user.id)
            )
        await callback.answer(
            "Повторы остановлены" if changed else "Сеанс не найден", show_alert=not changed
        )

    return router
