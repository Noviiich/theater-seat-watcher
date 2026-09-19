from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from theater_tickets.adapters.quicktickets.checkout import (
    BrowserContactFormObservation,
    BrowserHoldObservation,
    FakeCheckoutTransport,
    MemoryCheckoutStageRecorder,
    QuickTicketsBrowserCheckoutTransport,
    QuickTicketsCheckoutAdapter,
)
from theater_tickets.application.checkout import (
    CheckoutBuyer,
    CheckoutErrorCode,
    CheckoutRequest,
    CheckoutStage,
    CheckoutState,
    ProviderCheckoutObservation,
)
from theater_tickets.domain.models import BookingMode, Money, SessionKey


def _held_at() -> datetime:
    return datetime(2026, 9, 19, 16, 30, tzinfo=UTC)


def _confirmed() -> ProviderCheckoutObservation:
    held_at = _held_at()
    return ProviderCheckoutObservation(
        CheckoutState.CONFIRMED,
        provider_order_id="provider-order",
        seat_ids=("seat-1", "seat-2"),
        total=Money(4_000),
        payment_url="https://quicktickets.ru/payment/order/secret-token",
        held_at=held_at,
        expires_at=held_at + timedelta(seconds=1200),
        payment_url_transferable=True,
    )


def test_dry_run_never_calls_write_transport() -> None:
    transport = FakeCheckoutTransport([])
    recorder = MemoryCheckoutStageRecorder()

    result = asyncio.run(_adapter(BookingMode.DRY_RUN, transport, recorder).submit(_request()))

    assert result.state is CheckoutState.DRY_RUN
    assert transport.calls == []
    assert recorder.records == []


def test_confirmed_order_requires_and_preserves_exact_contract() -> None:
    observation = _confirmed()
    transport = FakeCheckoutTransport(
        [observation],
        intermediate_stages=(
            CheckoutStage.HOLD_CREATED,
            CheckoutStage.CONTACT_FORM_RECEIVED,
            CheckoutStage.CONFIRM_STARTED,
        ),
    )
    recorder = MemoryCheckoutStageRecorder()

    result = asyncio.run(_adapter(BookingMode.LIVE, transport, recorder).submit(_request()))

    assert result.state is CheckoutState.CONFIRMED
    assert result.order is not None
    assert result.order.provider_order_id == "provider-order"
    assert result.order.seat_ids == ("seat-1", "seat-2")
    assert result.order.total == Money(4_000)
    assert [stage for _, stage in recorder.records] == [
        CheckoutStage.WRITE_STARTED,
        CheckoutStage.HOLD_CREATED,
        CheckoutStage.CONTACT_FORM_RECEIVED,
        CheckoutStage.CONFIRM_STARTED,
        CheckoutStage.RESPONSE_RECEIVED,
        CheckoutStage.VALIDATED,
    ]


@pytest.mark.parametrize(
    ("observation", "error_code"),
    [
        (
            replace(_confirmed(), seat_ids=("seat-1",)),
            CheckoutErrorCode.PARTIAL_RESULT,
        ),
        (
            replace(_confirmed(), seat_ids=("seat-1", "other")),
            CheckoutErrorCode.SEAT_MISMATCH,
        ),
        (
            replace(_confirmed(), total=Money(4_100)),
            CheckoutErrorCode.AMOUNT_MISMATCH,
        ),
        (
            replace(_confirmed(), provider_order_id=None),
            CheckoutErrorCode.PARTIAL_RESULT,
        ),
        (
            replace(_confirmed(), payment_url=None),
            CheckoutErrorCode.PAYMENT_URL_MISSING,
        ),
        (
            replace(_confirmed(), payment_url="https://quicktickets.ru/theatre/s3159"),
            CheckoutErrorCode.INVALID_PAYMENT_URL,
        ),
        (
            replace(_confirmed(), payment_url="http://quicktickets.ru/payment/order/token"),
            CheckoutErrorCode.INVALID_PAYMENT_URL,
        ),
        (
            replace(_confirmed(), payment_url="https://evil.example/payment/order/token"),
            CheckoutErrorCode.INVALID_PAYMENT_URL,
        ),
        (
            replace(_confirmed(), payment_url_transferable=False),
            CheckoutErrorCode.INVALID_PAYMENT_URL,
        ),
        (
            replace(_confirmed(), expires_at=None),
            CheckoutErrorCode.EXPIRY_MISSING,
        ),
        (
            replace(_confirmed(), expires_at=_held_at()),
            CheckoutErrorCode.CONTRACT_CHANGED,
        ),
    ],
)
def test_invalid_or_partial_success_is_never_a_confirmed_order(
    observation: ProviderCheckoutObservation, error_code: CheckoutErrorCode
) -> None:
    result = asyncio.run(
        _adapter(
            BookingMode.LIVE,
            FakeCheckoutTransport([observation]),
            MemoryCheckoutStageRecorder(),
        ).submit(_request())
    )

    assert result.state is CheckoutState.AMBIGUOUS
    assert result.error_code is error_code
    assert result.order is None


@pytest.mark.parametrize(
    ("observation", "expected_state", "expected_code"),
    [
        (
            ProviderCheckoutObservation(
                CheckoutState.REJECTED,
                error_code=CheckoutErrorCode.SEAT_CONFLICT,
            ),
            CheckoutState.REJECTED,
            CheckoutErrorCode.SEAT_CONFLICT,
        ),
        (
            ProviderCheckoutObservation(
                CheckoutState.REJECTED,
                error_code=CheckoutErrorCode.AUTH_EXPIRED,
            ),
            CheckoutState.REJECTED,
            CheckoutErrorCode.AUTH_EXPIRED,
        ),
        (
            ProviderCheckoutObservation(
                CheckoutState.REQUIRES_USER_ACTION,
                error_code=CheckoutErrorCode.CAPTCHA,
            ),
            CheckoutState.REQUIRES_USER_ACTION,
            CheckoutErrorCode.CAPTCHA,
        ),
        (
            ProviderCheckoutObservation(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.PARTIAL_RESULT,
            ),
            CheckoutState.AMBIGUOUS,
            CheckoutErrorCode.PARTIAL_RESULT,
        ),
    ],
)
def test_provider_failures_remain_typed(
    observation: ProviderCheckoutObservation,
    expected_state: CheckoutState,
    expected_code: CheckoutErrorCode,
) -> None:
    result = asyncio.run(
        _adapter(
            BookingMode.LIVE,
            FakeCheckoutTransport([observation]),
            MemoryCheckoutStageRecorder(),
        ).submit(_request())
    )

    assert result.state is expected_state
    assert result.error_code is expected_code


def test_timeout_after_write_start_is_ambiguous_and_not_retried() -> None:
    transport = FakeCheckoutTransport([TimeoutError(), _confirmed()])
    recorder = MemoryCheckoutStageRecorder()

    result = asyncio.run(_adapter(BookingMode.LIVE, transport, recorder).submit(_request()))

    assert result.state is CheckoutState.AMBIGUOUS
    assert result.error_code is CheckoutErrorCode.TRANSPORT_TIMEOUT
    assert len(transport.calls) == 1
    assert [stage for _, stage in recorder.records] == [
        CheckoutStage.WRITE_STARTED,
        CheckoutStage.AMBIGUOUS,
    ]


def test_browser_transport_records_each_changing_stage_before_confirm() -> None:
    driver = _FakeBrowserDriver()
    recorder = MemoryCheckoutStageRecorder()
    transport = QuickTicketsBrowserCheckoutTransport(driver)

    observation = asyncio.run(transport.submit(_request(), recorder=recorder))

    assert observation.state is CheckoutState.CONFIRMED
    assert driver.calls == ["initialize_hold", "open_contact_form", "submit_contact_form"]
    assert [stage for _, stage in recorder.records] == [
        CheckoutStage.HOLD_CREATED,
        CheckoutStage.CONTACT_FORM_RECEIVED,
        CheckoutStage.CONFIRM_STARTED,
    ]


@pytest.mark.parametrize(
    ("hold", "form", "state", "error_code", "expected_calls"),
    [
        (
            BrowserHoldObservation(False, (), CheckoutErrorCode.SEAT_CONFLICT),
            BrowserContactFormObservation(frozenset(), False),
            CheckoutState.REJECTED,
            CheckoutErrorCode.SEAT_CONFLICT,
            ["initialize_hold"],
        ),
        (
            BrowserHoldObservation(True, ("seat-1",)),
            BrowserContactFormObservation(frozenset(), False),
            CheckoutState.AMBIGUOUS,
            CheckoutErrorCode.PARTIAL_RESULT,
            ["initialize_hold"],
        ),
        (
            BrowserHoldObservation(True, ("seat-1", "seat-2")),
            BrowserContactFormObservation(frozenset(), False, captcha_present=True),
            CheckoutState.REQUIRES_USER_ACTION,
            CheckoutErrorCode.CAPTCHA,
            ["initialize_hold", "open_contact_form"],
        ),
        (
            BrowserHoldObservation(True, ("seat-1", "seat-2")),
            BrowserContactFormObservation(frozenset({"email"}), False),
            CheckoutState.REQUIRES_USER_ACTION,
            CheckoutErrorCode.CONTRACT_CHANGED,
            ["initialize_hold", "open_contact_form"],
        ),
    ],
)
def test_browser_transport_stops_before_unsafe_next_stage(
    hold: BrowserHoldObservation,
    form: BrowserContactFormObservation,
    state: CheckoutState,
    error_code: CheckoutErrorCode,
    expected_calls: list[str],
) -> None:
    driver = _FakeBrowserDriver(hold=hold, form=form)

    result = asyncio.run(
        QuickTicketsBrowserCheckoutTransport(driver).submit(
            _request(), recorder=MemoryCheckoutStageRecorder()
        )
    )

    assert result.state is state
    assert result.error_code is error_code
    assert driver.calls == expected_calls


def test_secrets_are_not_exposed_by_repr() -> None:
    request = _request()
    result = asyncio.run(
        _adapter(
            BookingMode.LIVE,
            FakeCheckoutTransport([_confirmed()]),
            MemoryCheckoutStageRecorder(),
        ).submit(request)
    )

    rendered = repr((request, request.buyer, result, result.order))
    assert "buyer@example.test" not in rendered
    assert "/payment/order/secret-token" not in rendered


def _adapter(
    mode: BookingMode,
    transport: FakeCheckoutTransport,
    recorder: MemoryCheckoutStageRecorder,
) -> QuickTicketsCheckoutAdapter:
    return QuickTicketsCheckoutAdapter(
        booking_mode=mode,
        transport=transport,
        recorder=recorder,
    )


class _FakeBrowserDriver:
    def __init__(
        self,
        *,
        hold: BrowserHoldObservation | None = None,
        form: BrowserContactFormObservation | None = None,
    ) -> None:
        self.hold = hold or BrowserHoldObservation(True, ("seat-1", "seat-2"))
        self.form = form or BrowserContactFormObservation(
            frozenset({"email", "lastname", "phone", "personalDataConsent"}),
            True,
        )
        self.calls: list[str] = []

    async def initialize_hold(self, request: CheckoutRequest) -> BrowserHoldObservation:
        self.calls.append("initialize_hold")
        return self.hold

    async def open_contact_form(self) -> BrowserContactFormObservation:
        self.calls.append("open_contact_form")
        return self.form

    async def submit_contact_form(self, request: CheckoutRequest) -> ProviderCheckoutObservation:
        self.calls.append("submit_contact_form")
        return _confirmed()


def _request() -> CheckoutRequest:
    return CheckoutRequest(
        intent_id="intent",
        session_key=SessionKey("quicktickets", "orel-teatr", "3159"),
        seat_ids=("seat-1", "seat-2"),
        expected_total=Money(4_000),
        reserved_total=Money(4_500),
        expected_hold_ttl_seconds=1200,
        buyer=CheckoutBuyer(
            lastname="Tester",
            firstname="Test",
            middlename="Example",
            email="buyer@example.test",
            phone="+79990000000",
            personal_data_consent=True,
        ),
    )
