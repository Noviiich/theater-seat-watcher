from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from theater_tickets.adapters.quicktickets.checkout import (
    MemoryCheckoutStageRecorder,
    QuickTicketsCheckoutAdapter,
)
from theater_tickets.adapters.quicktickets.reconciliation import (
    UnsupportedQuickTicketsReconciliationTransport,
)
from theater_tickets.application.checkout import (
    CheckoutBuyer,
    CheckoutErrorCode,
    CheckoutRequest,
    CheckoutStageRecorder,
    CheckoutState,
    ProviderCheckoutObservation,
)
from theater_tickets.application.reconciliation import (
    CheckoutRecoveryService,
    IncompleteCheckout,
    ManualResolution,
    ReconciliationObservation,
    ReconciliationState,
    RecoveryDisposition,
    RecoveryOutcome,
)
from theater_tickets.domain.models import BookingMode, Money, SessionKey


def _confirmed() -> ProviderCheckoutObservation:
    held_at = datetime(2026, 9, 19, 16, 30, tzinfo=UTC)
    return ProviderCheckoutObservation(
        CheckoutState.CONFIRMED,
        provider_order_id="order",
        seat_ids=("seat-1", "seat-2"),
        total=Money(4_000),
        payment_url="https://quicktickets.ru/payment/order/opaque",
        held_at=held_at,
        expires_at=held_at + timedelta(seconds=1200),
        payment_url_transferable=True,
    )


def test_crash_after_remote_creation_reconciles_without_second_order() -> None:
    request = _request()
    provider = _FakeProvider(_confirmed())
    recorder = MemoryCheckoutStageRecorder()
    validator = QuickTicketsCheckoutAdapter(
        booking_mode=BookingMode.LIVE,
        transport=provider,
        recorder=recorder,
    )

    submit_result = asyncio.run(validator.submit(request))
    repository = _Repository((IncompleteCheckout("intent", True),))
    recovery = CheckoutRecoveryService(
        repository=repository,
        request_loader=_Loader(request),
        transport=provider,
        validator=validator,
    )
    outcomes = asyncio.run(recovery.recover_startup())

    assert submit_result.state is CheckoutState.AMBIGUOUS
    assert outcomes[0].disposition is RecoveryDisposition.CONFIRMED
    assert provider.created_orders == 1
    assert provider.reconciliation_calls == 1
    assert repository.applied == list(outcomes)


def test_crash_before_write_is_retryable_without_provider_lookup() -> None:
    request = _request()
    repository = _Repository((IncompleteCheckout("intent", False),))
    provider = _FakeProvider(_confirmed())
    recovery = CheckoutRecoveryService(
        repository=repository,
        request_loader=_Loader(request),
        transport=provider,
        validator=_validator(provider),
    )

    outcome = asyncio.run(recovery.recover_startup())[0]

    assert outcome.disposition is RecoveryDisposition.RETRY_ALLOWED
    assert provider.reconciliation_calls == 0


@pytest.mark.parametrize(
    ("observation", "disposition", "error_code"),
    [
        (
            ReconciliationObservation(ReconciliationState.NOT_FOUND),
            RecoveryDisposition.RETRY_ALLOWED,
            None,
        ),
        (
            ReconciliationObservation(ReconciliationState.UNKNOWN),
            RecoveryDisposition.NEEDS_ATTENTION,
            CheckoutErrorCode.PARTIAL_RESULT,
        ),
        (
            ReconciliationObservation(ReconciliationState.UNSUPPORTED),
            RecoveryDisposition.NEEDS_ATTENTION,
            CheckoutErrorCode.UNSUPPORTED,
        ),
        (
            ReconciliationObservation(
                ReconciliationState.FOUND,
                replace(_confirmed(), payment_url=None),
            ),
            RecoveryDisposition.NEEDS_ATTENTION,
            CheckoutErrorCode.PAYMENT_URL_MISSING,
        ),
    ],
)
def test_reconciliation_outcomes_are_explicit(
    observation: ReconciliationObservation,
    disposition: RecoveryDisposition,
    error_code: CheckoutErrorCode | None,
) -> None:
    request = _request()
    repository = _Repository((IncompleteCheckout("intent", True),))
    transport = _ReconciliationOnly(observation)
    recovery = CheckoutRecoveryService(
        repository=repository,
        request_loader=_Loader(request),
        transport=transport,
        validator=_validator(_FakeProvider(_confirmed())),
    )

    outcome = asyncio.run(recovery.recover_startup())[0]

    assert outcome.disposition is disposition
    assert outcome.error_code is error_code


def test_reconciliation_timeout_needs_attention() -> None:
    request = _request()
    recovery = CheckoutRecoveryService(
        repository=_Repository((IncompleteCheckout("intent", True),)),
        request_loader=_Loader(request),
        transport=_ReconciliationOnly(TimeoutError()),
        validator=_validator(_FakeProvider(_confirmed())),
    )

    outcome = asyncio.run(recovery.recover_startup())[0]

    assert outcome.disposition is RecoveryDisposition.NEEDS_ATTENTION
    assert outcome.error_code is CheckoutErrorCode.TRANSPORT_TIMEOUT


def test_quicktickets_reconciliation_is_safely_unsupported_without_lookup_contract() -> None:
    observation = asyncio.run(
        UnsupportedQuickTicketsReconciliationTransport().reconcile(_request())
    )

    assert observation.state is ReconciliationState.UNSUPPORTED


def test_manual_resolution_can_only_allow_retry_or_keep_blocked() -> None:
    request = _request()
    repository = _Repository(())
    recovery = CheckoutRecoveryService(
        repository=repository,
        request_loader=_Loader(request),
        transport=_ReconciliationOnly(ReconciliationObservation(ReconciliationState.UNSUPPORTED)),
        validator=_validator(_FakeProvider(_confirmed())),
    )

    retry = asyncio.run(recovery.resolve_manually("intent", ManualResolution.ORDER_AND_HOLD_ABSENT))
    blocked = asyncio.run(recovery.resolve_manually("intent", ManualResolution.KEEP_BLOCKED))

    assert retry.disposition is RecoveryDisposition.RETRY_ALLOWED
    assert blocked.disposition is RecoveryDisposition.NEEDS_ATTENTION
    assert all(outcome.checkout_result is None for outcome in (retry, blocked))


class _Repository:
    def __init__(self, incomplete: tuple[IncompleteCheckout, ...]) -> None:
        self.incomplete = incomplete
        self.applied: list[RecoveryOutcome] = []

    async def list_incomplete(self) -> tuple[IncompleteCheckout, ...]:
        return self.incomplete

    async def apply(self, outcome: RecoveryOutcome) -> None:
        self.applied.append(outcome)


class _Loader:
    def __init__(self, request: CheckoutRequest) -> None:
        self.request = request
        self.calls = 0

    async def load(self, intent_id: str) -> CheckoutRequest:
        self.calls += 1
        return self.request


class _FakeProvider:
    def __init__(self, confirmed: ProviderCheckoutObservation) -> None:
        self.confirmed = confirmed
        self.created_orders = 0
        self.reconciliation_calls = 0

    async def submit(
        self, request: CheckoutRequest, *, recorder: CheckoutStageRecorder
    ) -> ProviderCheckoutObservation:
        self.created_orders += 1
        raise TimeoutError

    async def reconcile(self, request: CheckoutRequest) -> ReconciliationObservation:
        self.reconciliation_calls += 1
        return ReconciliationObservation(ReconciliationState.FOUND, self.confirmed)


class _ReconciliationOnly:
    def __init__(self, result: ReconciliationObservation | BaseException) -> None:
        self.result = result

    async def reconcile(self, request: CheckoutRequest) -> ReconciliationObservation:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _validator(provider: _FakeProvider) -> QuickTicketsCheckoutAdapter:
    return QuickTicketsCheckoutAdapter(
        booking_mode=BookingMode.LIVE,
        transport=provider,
        recorder=MemoryCheckoutStageRecorder(),
    )


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
