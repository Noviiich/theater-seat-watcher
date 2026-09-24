from __future__ import annotations

from decimal import Decimal

import pytest

from theater_tickets.adapters.quicktickets.browser import (
    parse_calculation_quote,
    parse_calculation_total,
    parse_confirm_response,
    parse_init_response,
)
from theater_tickets.domain.models import Money


def test_init_response_requires_exact_codes_and_selected_count() -> None:
    assert parse_init_response(
        {
            "result": "success",
            "data": {"anyticketsCodes": ["private-code"], "selectAnyplacesCount": 1},
        }
    ) == (("private-code",), 1)

    with pytest.raises(ValueError):
        parse_init_response({"result": "error", "data": {}})


def test_calculation_uses_exact_total_including_commission() -> None:
    assert parse_calculation_total(
        {"result": "success", "data": {"amount": 700, "commission": 35, "total": Decimal("735")}}
    ) == Money(73_500)

    with pytest.raises(ValueError):
        parse_calculation_total({"result": "success", "data": {"amount": 700}})


def test_calculation_quote_requires_all_exact_fields() -> None:
    payload = {
        "result": "success",
        "data": {"amount": 700, "commission": 35, "total": Decimal("735")},
    }
    assert parse_calculation_quote(payload) == (Money(70_000), Money(3_500), Money(73_500))
    with pytest.raises(ValueError):
        parse_calculation_quote({"result": "success", "data": {"total": 735}})


def test_confirm_accepts_only_quicktickets_payment_handoff() -> None:
    url = "https://quicktickets.ru/payment/order/private-code"
    assert parse_confirm_response({"result": "success", "data": {"url": url}}) == url

    for invalid in (
        "http://quicktickets.ru/payment/order/private-code",
        "https://evil.example/payment/order/private-code",
        "https://quicktickets.ru/session/3159",
    ):
        with pytest.raises(ValueError):
            parse_confirm_response({"result": "success", "data": {"url": invalid}})
