"""Subscription commands and owner-scoped callbacks."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theater_tickets.adapters.persistence.access import SqlAlchemyTelegramAccess
from theater_tickets.adapters.persistence.repositories import SubscriptionRepository
from theater_tickets.application.checkout import CheckoutBuyer
from theater_tickets.application.status import StatusReader, render_status, render_user_status
from theater_tickets.domain.errors import DomainValidationError
from theater_tickets.domain.models import BookingMode, Money, RenewalPolicy, Subscription
from theater_tickets.settings import BookingMode as RuntimeBookingMode

THEATRE_ALIAS = "orel-teatr-svobodnoe-prostranstvo"
MENU_PROFILE = "👤 Мой профиль"
MENU_SUBSCRIPTIONS = "🎭 Мои подписки"
MENU_CREATE_SUBSCRIPTION = "➕ Новая подписка"
MENU_STATUS = "📊 Статус"
MENU_HELP = "❓ Помощь"


class ProfileForm(StatesGroup):
    lastname = State()
    firstname = State()
    middlename = State()
    email = State()
    phone = State()
    consent = State()


class SubscriptionForm(StatesGroup):
    selecting_ticket_count = State()
    entering_ticket_limit = State()
    entering_order_limit = State()
    confirming_live = State()


def _menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_PROFILE), KeyboardButton(text=MENU_SUBSCRIPTIONS)],
            [KeyboardButton(text=MENU_CREATE_SUBSCRIPTION), KeyboardButton(text=MENU_STATUS)],
            [KeyboardButton(text=MENU_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def _profile_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Заполнить или изменить", callback_data="profile:edit")],
        ]
    )


def _consent_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Согласен", callback_data="profile:consent"),
                InlineKeyboardButton(text="Отмена", callback_data="profile:cancel"),
            ],
        ]
    )


def _access_request_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Запросить доступ", callback_data="access:request")],
        ]
    )


def _access_decision_actions(telegram_user_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Разрешить", callback_data=f"access:grant:{telegram_user_id}"
                ),
                InlineKeyboardButton(
                    text="Отклонить", callback_data=f"access:deny:{telegram_user_id}"
                ),
            ],
        ]
    )


def _ticket_count_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="70 билетов — каждый отдельным заказом",
                    callback_data="subscribe:organization",
                )
            ]
        ]
        + [
            [
                InlineKeyboardButton(text=str(count), callback_data=f"subscribe:count:{count}")
                for count in range(first, first + 3)
            ]
            for first in (1, 4)
        ]
        + [[InlineKeyboardButton(text="Отмена", callback_data="subscribe:cancel")]]
    )


def _subscription_actions(subscription: Subscription) -> InlineKeyboardMarkup:
    action = "pause" if subscription.enabled else "resume"
    label = "Остановить" if subscription.enabled else "Возобновить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"subscription:{action}:{subscription.subscription_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Удалить",
                    callback_data=f"subscription:delete:{subscription.subscription_id}",
                )
            ],
        ]
    )


def _delete_confirmation(subscription_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить",
                    callback_data=f"subscription:delete-confirm:{subscription_id}",
                ),
                InlineKeyboardButton(
                    text="Отмена", callback_data=f"subscription:delete-cancel:{subscription_id}"
                ),
            ]
        ]
    )


def _help(*, live: bool = False) -> str:
    return "Пользуйтесь кнопками внизу чата. Начните с «Мой профиль», затем создайте подписку. " + (
        "Реальная подписка требует отдельного подтверждения."
        if live
        else "В dry_run бот только наблюдает и подбирает места, не бронируя их."
    )


def _profile_summary(profile: CheckoutBuyer | None) -> str:
    if profile is None:
        return (
            "Профиль для оплаты пока не заполнен.\n"
            "Нажмите кнопку «Заполнить или изменить» ниже. Данные понадобятся только "
            "для будущего оформления билетов."
        )
    # Access middleware and owner-scoped queries keep these data private.
    return (
        "Ваш профиль для оплаты:\n"
        f"ФИО: {profile.lastname} {profile.firstname} {profile.middlename}\n"
        f"Почта: {profile.email}\n"
        f"Телефон: {profile.phone}\n\n"
        "Чтобы изменить данные, нажмите кнопку ниже."
    )


def _profile_values(command: CommandObject) -> tuple[str, str, str, str, str] | str:
    argument = (command.args or "").strip()
    if not argument:
        return ""
    command_name, _, raw_values = argument.partition(" ")
    if command_name.casefold() not in {"set", "edit"}:
        return "Используйте: /profile set Фамилия | Имя | Отчество | email | телефон."
    values = tuple(part.strip() for part in raw_values.split("|"))
    if len(values) != 5:
        return (
            "Нужно указать пять значений через символ |: "
            "Фамилия | Имя | Отчество | email | телефон."
        )
    return values


def _summary(items: tuple[Subscription, ...]) -> str:
    if not items:
        return "Подписок пока нет. Нажмите «➕ Новая подписка» в меню ниже."
    return "\n\n".join(
        _subscription_line(item, position=position) for position, item in enumerate(items, start=1)
    )


def _subscription_line(item: Subscription, *, position: int) -> str:
    count = item.ticket_count
    if 11 <= count % 100 <= 14:
        places = "мест"
    elif count % 10 == 1:
        places = "место"
    elif count % 10 in (2, 3, 4):
        places = "места"
    else:
        places = "мест"
    lines = [
        f"🎭 Подписка №{position}: {count} {places}"
        + (", по одному в заказе" if item.individual_orders else " рядом"),
        "🟢 Поиск включён" if item.enabled else "⏸ Поиск на паузе",
    ]
    if item.booking_mode is BookingMode.LIVE:
        lines.append(
            "На каждом новом сеансе оформлю до 70 отдельных заказов и пришлю ссылку на каждый."
            if item.individual_orders
            else "Для каждого нового подходящего сеанса попробую оформить отдельный заказ "
            "и пришлю ссылку на оплату."
        )
    else:
        lines.append("Покажу результаты поиска. Билеты пока не бронирую.")
    if item.max_ticket_price is not None:
        lines.append(f"Цена билета на каждом сеансе — до {_rubles(item.max_ticket_price)} ₽.")
    if item.max_order_total is not None:
        lines.append(f"Каждый заказ — до {_rubles(item.max_order_total)} ₽.")
    elif item.individual_orders:
        lines.append("Без ограничений по цене и соседству. Ограничения продавца сохраняются.")
    return "\n".join(lines)


def _parse_limit(value: str | None) -> Money | None:
    raw = (value or "").strip().replace(",", ".")
    if not raw or len(raw) > 13 or any(char not in "0123456789." for char in raw):
        return None
    try:
        amount = Money.from_rubles(Decimal(raw))
    except (ValueError, DomainValidationError):
        return None
    return amount if 0 < amount.minor_units <= 100_000_000_000 else None


def _rubles(value: Money) -> str:
    return f"{Decimal(value.minor_units) / 100:g}"


def _new_button_subscription(
    ticket_count: int,
    user_id: str,
    *,
    live: bool = False,
    max_ticket_price: Money | None = None,
    max_order_total: Money | None = None,
    individual_orders: bool = False,
) -> Subscription:
    if live and not individual_orders and (max_ticket_price is None or max_order_total is None):
        raise ValueError("live subscription requires monetary limits")
    if (
        live
        and max_ticket_price is not None
        and max_order_total is not None
        and (max_ticket_price.minor_units <= 0 or max_order_total.minor_units <= 0)
    ):
        raise ValueError("live subscription requires positive limits")
    return Subscription(
        subscription_id=str(uuid4()),
        buyer_id=user_id,
        theatre_alias=THEATRE_ALIAS,
        ticket_count=ticket_count,
        individual_orders=individual_orders,
        seat_profile_id="auto",
        max_sessions_per_batch=None,
        booking_mode=BookingMode.LIVE if live else BookingMode.DRY_RUN,
        max_ticket_price=max_ticket_price,
        max_order_total=max_order_total,
        max_batch_total=None,
        max_active_orders=None,
        max_active_total=None,
        renewal_policy=RenewalPolicy(),
    )


def build_subscription_router(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status_reader: StatusReader | None = None,
    now: Callable[[], datetime] | None = None,
    catalogue_stale_after_seconds: int = 180,
    administrator_user_id: str,
    access: SqlAlchemyTelegramAccess,
    booking_mode: RuntimeBookingMode = RuntimeBookingMode.DRY_RUN,
) -> Router:
    router = Router(name="subscriptions")
    clock = now or (lambda: datetime.now(UTC))

    @router.message(Command("start", "help"))
    async def help_command(message: Message) -> None:
        assert message.from_user is not None
        user_id = str(message.from_user.id)
        if user_id != administrator_user_id and not await access.is_granted(user_id):
            await message.answer(
                "Это закрытый бот. Нажмите кнопку, чтобы отправить запрос администратору.",
                reply_markup=_access_request_actions(),
            )
            return
        await message.answer(
            _help(live=booking_mode is RuntimeBookingMode.LIVE),
            reply_markup=_menu(),
        )

    @router.callback_query(F.data == "access:request")
    async def request_access(callback: CallbackQuery) -> None:
        assert callback.from_user is not None and isinstance(callback.message, Message)
        assert callback.bot is not None
        user_id = str(callback.from_user.id)
        notify_administrator = await access.request(
            telegram_user_id=user_id,
            telegram_chat_id=str(callback.message.chat.id),
        )
        if notify_administrator:
            username = callback.from_user.username or "без username"
            await callback.bot.send_message(
                chat_id=administrator_user_id,
                text=(
                    f"Запрос доступа от {callback.from_user.full_name} (@{username}), ID {user_id}."
                ),
                reply_markup=_access_decision_actions(user_id),
            )
        await callback.message.answer("Запрос отправлен администратору. Ожидайте решения.")
        await callback.answer()

    @router.callback_query(F.data.startswith("access:grant:") | F.data.startswith("access:deny:"))
    async def decide_access(callback: CallbackQuery) -> None:
        assert isinstance(callback.message, Message)
        assert callback.bot is not None
        granted = (callback.data or "").startswith("access:grant:")
        user_id = (callback.data or "").rsplit(":", maxsplit=1)[-1]
        chat_id = await access.decide(telegram_user_id=user_id, granted=granted)
        if chat_id is None:
            await callback.answer("Запрос уже обработан", show_alert=True)
            return
        await callback.bot.send_message(
            chat_id=chat_id,
            text="Доступ разрешён. Отправьте /start, чтобы открыть меню."
            if granted
            else "Администратор отклонил запрос доступа.",
        )
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Доступ разрешён" if granted else "Запрос отклонён")

    async def show_profile(message: Message) -> None:
        assert message.from_user is not None
        async with session_factory() as session:
            profile = await SubscriptionRepository(session).buyer_profile(str(message.from_user.id))
        await message.answer(_profile_summary(profile), reply_markup=_profile_actions())

    @router.message(Command("profile"))
    async def profile(message: Message, command: CommandObject) -> None:
        assert message.from_user is not None
        values = _profile_values(command)
        async with session_factory() as session, session.begin():
            repository = SubscriptionRepository(session)
            if isinstance(values, str):
                profile = await repository.buyer_profile(str(message.from_user.id))
                await message.answer(
                    values or _profile_summary(profile),
                    reply_markup=_profile_actions() if not values else None,
                )
                return
            try:
                profile = await repository.save_buyer_profile(
                    telegram_user_id=str(message.from_user.id),
                    telegram_chat_id=str(message.chat.id),
                    lastname=values[0],
                    firstname=values[1],
                    middlename=values[2],
                    email=values[3],
                    phone=values[4],
                )
            except DomainValidationError:
                await message.answer(
                    "Не удалось сохранить профиль: проверьте, что все поля заполнены, "
                    "email корректен, а телефон содержит от 10 до 15 цифр."
                )
                return
        await message.answer(
            "Профиль сохранён.\n\n" + _profile_summary(profile),
            reply_markup=_profile_actions(),
        )

    @router.message(F.text == MENU_PROFILE)
    async def profile_from_menu(message: Message) -> None:
        await show_profile(message)

    @router.callback_query(F.data == "profile:edit")
    async def edit_profile(callback: CallbackQuery, state: FSMContext) -> None:
        assert callback.message is not None
        await state.clear()
        await state.set_state(ProfileForm.lastname)
        await callback.message.answer("Введите фамилию.")
        await callback.answer()

    @router.message(ProfileForm.lastname)
    async def profile_lastname(message: Message, state: FSMContext) -> None:
        await state.update_data(lastname=(message.text or "").strip())
        await state.set_state(ProfileForm.firstname)
        await message.answer("Теперь введите имя.")

    @router.message(ProfileForm.firstname)
    async def profile_firstname(message: Message, state: FSMContext) -> None:
        await state.update_data(firstname=(message.text or "").strip())
        await state.set_state(ProfileForm.middlename)
        await message.answer("Введите отчество.")

    @router.message(ProfileForm.middlename)
    async def profile_middlename(message: Message, state: FSMContext) -> None:
        await state.update_data(middlename=(message.text or "").strip())
        await state.set_state(ProfileForm.email)
        await message.answer("Введите адрес электронной почты.")

    @router.message(ProfileForm.email)
    async def profile_email(message: Message, state: FSMContext) -> None:
        await state.update_data(email=(message.text or "").strip())
        await state.set_state(ProfileForm.phone)
        await message.answer("Введите номер телефона, например +79990000000.")

    @router.message(ProfileForm.phone)
    async def profile_phone(message: Message, state: FSMContext) -> None:
        await state.update_data(phone=(message.text or "").strip())
        await state.set_state(ProfileForm.consent)
        await message.answer(
            "Подтвердите согласие на хранение этих данных и их использование для "
            "будущего оформления билетов.",
            reply_markup=_consent_actions(),
        )

    @router.callback_query(ProfileForm.consent, F.data == "profile:consent")
    async def save_profile(callback: CallbackQuery, state: FSMContext) -> None:
        assert callback.from_user is not None and callback.message is not None
        values = await state.get_data()
        try:
            async with session_factory() as session, session.begin():
                profile = await SubscriptionRepository(session).save_buyer_profile(
                    telegram_user_id=str(callback.from_user.id),
                    telegram_chat_id=str(callback.message.chat.id),
                    lastname=str(values.get("lastname", "")),
                    firstname=str(values.get("firstname", "")),
                    middlename=str(values.get("middlename", "")),
                    email=str(values.get("email", "")),
                    phone=str(values.get("phone", "")),
                )
        except DomainValidationError:
            await state.clear()
            await callback.message.answer(
                "Не удалось сохранить профиль. Начните заполнение ещё раз и проверьте "
                "поля, email и телефон.",
                reply_markup=_profile_actions(),
            )
            await callback.answer()
            return
        await state.clear()
        await callback.message.answer(
            "Профиль сохранён.\n\n" + _profile_summary(profile),
            reply_markup=_profile_actions(),
        )
        await callback.answer("Сохранено")

    @router.callback_query(F.data == "profile:cancel")
    async def cancel_profile(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Заполнение отменено")

    @router.message(Command("subscribe"))
    async def subscribe(message: Message, state: FSMContext) -> None:
        await create_subscription_from_menu(message, state)

    @router.message(F.text == MENU_CREATE_SUBSCRIPTION)
    async def create_subscription_from_menu(message: Message, state: FSMContext) -> None:
        assert message.from_user is not None
        await state.clear()
        await state.set_state(SubscriptionForm.selecting_ticket_count)
        live = booking_mode is RuntimeBookingMode.LIVE
        await message.answer(
            "Для организации: 70 билетов, каждый отдельным заказом, без ограничений по цене "
            "и соседству. Либо выберите ниже количество соседних мест для одного заказа.\n\n"
            + (
                "Перед реальным оформлением потребуется подтверждение."
                if live
                else "Бот не оформит заказ. Лимиты нужны для подбора мест."
            ),
            reply_markup=_ticket_count_actions(),
        )

    @router.callback_query(
        SubscriptionForm.selecting_ticket_count, F.data == "subscribe:organization"
    )
    async def create_organization_subscription(callback: CallbackQuery, state: FSMContext) -> None:
        assert isinstance(callback.message, Message)
        live = booking_mode is RuntimeBookingMode.LIVE
        await state.update_data(ticket_count=70, individual_orders=True)
        await state.set_state(SubscriptionForm.confirming_live)
        await callback.message.answer(
            "Подписка для организации: до 70 билетов на каждый новый сеанс. "
            "Один билет — один заказ — отдельное сообщение со ссылкой на оплату. "
            "Любые свободные места, без ограничения цены и общей суммы. "
            "Если мест меньше, оформлю доступные и продолжу поиск до начала сеанса. "
            "Ограничения продавца не обходятся разделением заказов.\n\n"
            + (
                "Через 20 минут возможно повторное оформление только того же места, "
                "если оно свободно. Оплату выполняете вы; бот её не проверяет. "
                "Остановить все повторы можно паузой подписки, отдельное место — кнопкой у ссылки."
                if live
                else "Dry-run: только подбор, без реальных заказов."
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Подтверждаю 70 отдельных заказов"
                            if live
                            else "Создать dry-run на 70 билетов",
                            callback_data="subscribe:confirm",
                        )
                    ],
                    [InlineKeyboardButton(text="Отмена", callback_data="subscribe:cancel")],
                ]
            ),
        )
        await callback.answer()

    @router.callback_query(
        SubscriptionForm.selecting_ticket_count, F.data.startswith("subscribe:count:")
    )
    async def create_subscription_from_count(callback: CallbackQuery, state: FSMContext) -> None:
        assert callback.from_user is not None and isinstance(callback.message, Message)
        ticket_count = int((callback.data or "").removeprefix("subscribe:count:"))
        if ticket_count not in range(1, 7):
            await callback.answer("Выберите число от 1 до 6", show_alert=True)
            return
        if booking_mode is RuntimeBookingMode.LIVE:
            async with session_factory() as session:
                profile = await SubscriptionRepository(session).buyer_profile(
                    str(callback.from_user.id)
                )
            if profile is None:
                await state.clear()
                await callback.message.answer(
                    "Сначала заполните профиль для оплаты через «👤 Мой профиль»."
                )
                await callback.answer()
                return
        await state.update_data(ticket_count=ticket_count)
        await state.set_state(SubscriptionForm.entering_ticket_limit)
        await callback.message.answer(
            "Введите максимальную цену одного билета в рублях, например 2500 или 2500,50."
        )
        await callback.answer()

    @router.message(SubscriptionForm.entering_ticket_limit)
    async def enter_ticket_limit(message: Message, state: FSMContext) -> None:
        amount = _parse_limit(message.text)
        if amount is None:
            await message.answer(
                "Введите положительную сумму в рублях, не более 1 млрд ₽, с точностью до копейки."
            )
            return
        await state.update_data(ticket_limit_kopecks=amount.minor_units)
        await state.set_state(SubscriptionForm.entering_order_limit)
        await message.answer(
            "Введите максимальную сумму всего заказа в рублях, включая возможную комиссию."
        )

    @router.message(SubscriptionForm.entering_order_limit)
    async def enter_order_limit(message: Message, state: FSMContext) -> None:
        amount = _parse_limit(message.text)
        if amount is None:
            await message.answer(
                "Введите положительную сумму в рублях, не более 1 млрд ₽, с точностью до копейки."
            )
            return
        data = await state.get_data()
        ticket_minor = data.get("ticket_limit_kopecks")
        ticket_count = data.get("ticket_count")
        if not isinstance(ticket_minor, int) or not isinstance(ticket_count, int):
            await state.clear()
            await message.answer("Начните создание подписки заново.")
            return
        if amount.minor_units < ticket_minor:
            await message.answer(
                "Лимит заказа должен быть не меньше лимита одного билета. "
                "Введите сумму заказа ещё раз."
            )
            return
        await state.update_data(order_limit_kopecks=amount.minor_units)
        await state.set_state(SubscriptionForm.confirming_live)
        live = booking_mode is RuntimeBookingMode.LIVE
        await message.answer(
            f"Подтвердите подписку: {ticket_count} билет(а) на каждый новый подходящий сеанс.\n"
            f"Для каждого сеанса: не более {_rubles(Money(ticket_minor))} ₽ за билет "
            f"и {_rubles(amount)} ₽ за заказ.\n"
            + (
                "Реальное оформление: на каждом новом подходящем сеансе бот попробует "
                "оформить отдельный заказ на указанное число билетов. Лимит цены билета "
                "и суммы заказа применяется к каждому сеансу отдельно; общий расход по "
                "нескольким сеансам может быть выше лимита одного заказа. "
                "Для каждого оформленного сеанса бот будет создавать новый заказ и "
                "присылать новую ссылку примерно каждые 20 минут до остановки сеанса, "
                "паузы или удаления подписки либо начала сеанса. "
                "Оплату выполняете вы; бот не проверяет её автоматически."
                if live
                else "Режим dry_run: бот подбирает места без оформления заказа."
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Да, включить реальное оформление" if live else "Создать подписку",
                            callback_data="subscribe:confirm",
                        )
                    ],
                    [InlineKeyboardButton(text="Отмена", callback_data="subscribe:cancel")],
                ]
            ),
        )

    @router.callback_query(SubscriptionForm.confirming_live, F.data == "subscribe:confirm")
    async def confirm_live_subscription(callback: CallbackQuery, state: FSMContext) -> None:
        assert callback.from_user is not None and isinstance(callback.message, Message)
        live = booking_mode is RuntimeBookingMode.LIVE
        data = await state.get_data()
        ticket_count = data.get("ticket_count")
        ticket_minor = data.get("ticket_limit_kopecks")
        order_minor = data.get("order_limit_kopecks")
        individual_orders = data.get("individual_orders") is True
        if (individual_orders and ticket_count != 70) or (
            not individual_orders
            and (
                not isinstance(ticket_count, int)
                or ticket_count not in range(1, 7)
                or not isinstance(ticket_minor, int)
                or ticket_minor <= 0
                or not isinstance(order_minor, int)
                or order_minor < ticket_minor
                or ticket_minor > 100_000_000_000
                or order_minor > 100_000_000_000
            )
        ):
            await state.clear()
            await callback.answer("Начните создание заново", show_alert=True)
            return
        assert isinstance(ticket_count, int)
        if not individual_orders:
            assert isinstance(ticket_minor, int) and isinstance(order_minor, int)
        ticket_limit = (
            Money(ticket_minor) if isinstance(ticket_minor, int) and not individual_orders else None
        )
        order_limit = (
            Money(order_minor) if isinstance(order_minor, int) and not individual_orders else None
        )
        async with session_factory() as session, session.begin():
            repository = SubscriptionRepository(session)
            if live and await repository.buyer_profile(str(callback.from_user.id)) is None:
                await state.clear()
                await callback.answer("Сначала заполните профиль", show_alert=True)
                return
            value = _new_button_subscription(
                ticket_count,
                str(callback.from_user.id),
                live=live,
                max_ticket_price=ticket_limit,
                max_order_total=order_limit,
                individual_orders=individual_orders,
            )
            await repository.add(value, telegram_chat_id=str(callback.message.chat.id))
        await state.clear()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            ("Реальная подписка включена. " if live else "Подписка создана в dry_run. ")
            + "Первый снимок афиши станет точкой отсчёта. "
            "Остановить подписку можно в «🎭 Мои подписки»."
        )
        await callback.answer("Реальная подписка включена" if live else "Подписка создана")

    @router.callback_query(F.data == "subscribe:cancel")
    async def cancel_subscription(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Создание подписки отменено")

    @router.message(Command("preview"))
    async def preview(message: Message) -> None:
        await message.answer(
            "Предпросмотр доступен только после установки проверенного профиля зала и "
            "свежей схемы. Сейчас подбор не запускается."
        )

    @router.message(Command("subscriptions", "settings", "orders"))
    async def list_subscriptions(message: Message) -> None:
        assert message.from_user is not None
        async with session_factory() as session:
            items = await SubscriptionRepository(session).list_for_telegram_user(
                str(message.from_user.id)
            )
        if not items:
            await message.answer(_summary(items))
            return
        for position, item in enumerate(items, start=1):
            await message.answer(
                _subscription_line(item, position=position),
                reply_markup=_subscription_actions(item),
            )

    @router.message(F.text == MENU_SUBSCRIPTIONS)
    async def subscriptions_from_menu(message: Message) -> None:
        assert message.from_user is not None
        async with session_factory() as session:
            items = await SubscriptionRepository(session).list_for_telegram_user(
                str(message.from_user.id)
            )
        if not items:
            await message.answer(_summary(items))
            return
        for position, item in enumerate(items, start=1):
            await message.answer(
                _subscription_line(item, position=position),
                reply_markup=_subscription_actions(item),
            )

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        assert message.from_user is not None
        if status_reader is None:
            await message.answer("Runtime-диагностика ещё не подключена.")
            return
        current = clock()
        report = await status_reader.for_user(str(message.from_user.id), now=current)
        await message.answer(
            render_status(
                report,
                now=current,
                stale_after_seconds=catalogue_stale_after_seconds,
            )
        )

    @router.message(F.text == MENU_STATUS)
    async def status_from_menu(message: Message) -> None:
        assert message.from_user is not None
        if status_reader is None:
            await message.answer(
                "Сейчас не получается проверить состояние поиска. Попробуйте позже."
            )
            return
        current = clock()
        report = await status_reader.for_user(str(message.from_user.id), now=current)
        await message.answer(
            render_user_status(
                report, now=current, stale_after_seconds=catalogue_stale_after_seconds
            )
        )

    @router.message(F.text == MENU_HELP)
    async def help_from_menu(message: Message) -> None:
        assert message.from_user is not None
        await message.answer(
            _help(live=booking_mode is RuntimeBookingMode.LIVE),
            reply_markup=_menu(),
        )

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

    @router.callback_query(
        F.data.startswith("subscription:pause:") | F.data.startswith("subscription:resume:")
    )
    async def set_subscription_callback(callback: CallbackQuery) -> None:
        assert callback.from_user is not None and isinstance(callback.message, Message)
        _, action, subscription_id = (callback.data or "").split(":", maxsplit=2)
        enabled = action == "resume"
        async with session_factory() as session, session.begin():
            changed = await SubscriptionRepository(session).set_enabled(
                subscription_id=subscription_id,
                telegram_user_id=str(callback.from_user.id),
                enabled=enabled,
            )
        if not changed:
            await callback.answer("Подписка не найдена", show_alert=True)
            return
        label = "Остановить" if enabled else "Возобновить"
        next_action = "pause" if enabled else "resume"
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=label,
                            callback_data=f"subscription:{next_action}:{subscription_id}",
                        )
                    ],
                ]
            )
        )
        await callback.answer("Подписка возобновлена" if enabled else "Подписка остановлена")

    @router.callback_query(F.data.startswith("subscription:delete:"))
    async def request_delete_subscription(callback: CallbackQuery) -> None:
        assert isinstance(callback.message, Message)
        subscription_id = (callback.data or "").removeprefix("subscription:delete:")
        await callback.message.edit_reply_markup(reply_markup=_delete_confirmation(subscription_id))
        await callback.answer("Подтвердите удаление")

    @router.callback_query(F.data.startswith("subscription:delete-confirm:"))
    async def delete_subscription(callback: CallbackQuery) -> None:
        assert callback.from_user is not None and isinstance(callback.message, Message)
        subscription_id = (callback.data or "").removeprefix("subscription:delete-confirm:")
        async with session_factory() as session, session.begin():
            deleted = await SubscriptionRepository(session).delete(
                subscription_id=subscription_id,
                telegram_user_id=str(callback.from_user.id),
            )
        if deleted:
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Подписка удалена" if deleted else "Подписка не найдена")

    @router.callback_query(F.data.startswith("subscription:delete-cancel:"))
    async def cancel_delete_subscription(callback: CallbackQuery) -> None:
        assert callback.from_user is not None and isinstance(callback.message, Message)
        subscription_id = (callback.data or "").removeprefix("subscription:delete-cancel:")
        async with session_factory() as session:
            items = await SubscriptionRepository(session).list_for_telegram_user(
                str(callback.from_user.id)
            )
        subscription = next(
            (item for item in items if item.subscription_id == subscription_id), None
        )
        if subscription is not None:
            await callback.message.edit_reply_markup(
                reply_markup=_subscription_actions(subscription)
            )
        await callback.answer("Удаление отменено")

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
