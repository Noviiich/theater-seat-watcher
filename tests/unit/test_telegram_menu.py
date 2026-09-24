from __future__ import annotations

from aiogram.filters import CommandObject

from theater_tickets.adapters.telegram.routers.subscriptions import (
    MENU_CREATE_SUBSCRIPTION,
    MENU_HELP,
    MENU_PROFILE,
    MENU_STATUS,
    MENU_SUBSCRIPTIONS,
    _consent_actions,
    _menu,
    _new_button_subscription,
    _parse_limit,
    _profile_actions,
    _profile_values,
    _ticket_count_actions,
)
from theater_tickets.domain.models import BookingMode, Money


def test_main_menu_exposes_primary_actions_as_buttons() -> None:
    menu = _menu()

    assert [[button.text for button in row] for row in menu.keyboard] == [
        [MENU_PROFILE, MENU_SUBSCRIPTIONS],
        [MENU_CREATE_SUBSCRIPTION, MENU_STATUS],
        [MENU_HELP],
    ]


def test_profile_actions_offer_edit_consent_and_cancel() -> None:
    assert _profile_actions().inline_keyboard[0][0].callback_data == "profile:edit"
    assert [button.callback_data for button in _consent_actions().inline_keyboard[0]] == [
        "profile:consent",
        "profile:cancel",
    ]


def test_ticket_count_actions_offer_one_through_six_without_price_input() -> None:
    keyboard = _ticket_count_actions()

    assert [[button.text for button in row] for row in keyboard.inline_keyboard] == [
        ["1", "2", "3"],
        ["4", "5", "6"],
        ["Отмена"],
    ]


def test_live_subscription_has_all_limits_and_unlimited_cycles() -> None:
    subscription = _new_button_subscription(
        2,
        "123",
        live=True,
        max_ticket_price=Money.from_rubles("10000.50"),
        max_order_total=Money.from_rubles("50000.75"),
    )
    assert subscription.booking_mode is BookingMode.LIVE
    assert subscription.max_ticket_price == Money.from_rubles("10000.50")
    assert subscription.max_order_total == Money.from_rubles("50000.75")
    assert subscription.max_sessions_per_batch is None
    assert subscription.max_batch_total is None
    assert subscription.max_active_orders is None
    assert subscription.max_active_total is None
    assert subscription.renewal_policy.max_cycles_per_session is None
    assert subscription.renewal_policy.renewal_interval_seconds == 1200


def test_dry_run_subscription_keeps_owners_limits_for_selection() -> None:
    first = _new_button_subscription(
        2, "123", max_ticket_price=Money(150_000), max_order_total=Money(300_000)
    )
    second = _new_button_subscription(
        2, "456", max_ticket_price=Money(250_000), max_order_total=Money(500_000)
    )
    assert first.booking_mode is BookingMode.DRY_RUN
    assert first.buyer_id == "123"
    assert first.max_ticket_price == Money(150_000)
    assert first.max_order_total == Money(300_000)
    assert second.buyer_id == "456"
    assert second.max_ticket_price == Money(250_000)
    assert second.max_order_total == Money(500_000)


def test_limit_input_accepts_exact_kopecks_and_rejects_invalid_amounts() -> None:
    assert _parse_limit(" 2500,50 ") == Money(250_050)
    for value in (None, "", "0", "-1", "1.001", "nan", "1e3", "1000000000.01"):
        assert _parse_limit(value) is None


def test_profile_command_parser_accepts_editable_pipe_separated_values() -> None:
    command = CommandObject(
        prefix="/",
        command="profile",
        mention=None,
        args="set Иванов | Иван | Иванович | ivan@example.test | +79990000000",
    )

    assert _profile_values(command) == (
        "Иванов",
        "Иван",
        "Иванович",
        "ivan@example.test",
        "+79990000000",
    )
