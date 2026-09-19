"""Strict QuickTickets checkout validation and payment-link handoff."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

from theater_tickets.application.checkout import (
    CheckoutErrorCode,
    CheckoutRequest,
    CheckoutResult,
    CheckoutStage,
    CheckoutStageRecorder,
    CheckoutState,
    CheckoutWriteTransport,
    ConfirmedOrder,
    ProviderCheckoutObservation,
)
from theater_tickets.domain.models import BookingMode

_PAYMENT_PATH = re.compile(r"^/payment/order/[^/]+/?$")
_CONTACT_FIELDS = frozenset({"email", "lastname", "phone", "personalDataConsent"})


@dataclass(frozen=True, slots=True)
class BrowserHoldObservation:
    """Sanitized result of the observed initAnytickets hold operation."""

    accepted: bool
    seat_ids: tuple[str, ...]
    error_code: CheckoutErrorCode | None = None


@dataclass(frozen=True, slots=True)
class BrowserContactFormObservation:
    """Safe form metadata needed before buyer secrets may be submitted."""

    input_names: frozenset[str]
    submitter_ready: bool
    captcha_present: bool = False
    auth_expired: bool = False


class QuickTicketsBrowserDriver(Protocol):
    """Browser primitives whose selectors are kept inside the provider boundary."""

    async def initialize_hold(self, request: CheckoutRequest) -> BrowserHoldObservation:
        """Select the exact seats and perform the changing initAnytickets request once."""

    async def open_contact_form(self) -> BrowserContactFormObservation:
        """Open and inspect the observed `/ordering/confirm` form."""

    async def submit_contact_form(self, request: CheckoutRequest) -> ProviderCheckoutObservation:
        """Click the resolved real submitter once and inspect the payment handoff."""


class QuickTicketsBrowserCheckoutTransport:
    """Orchestrate the confirmed browser stages without retrying a write."""

    def __init__(self, driver: QuickTicketsBrowserDriver) -> None:
        self._driver = driver

    async def submit(
        self, request: CheckoutRequest, *, recorder: CheckoutStageRecorder
    ) -> ProviderCheckoutObservation:
        hold = await self._driver.initialize_hold(request)
        if not hold.accepted:
            return ProviderCheckoutObservation(
                CheckoutState.REJECTED,
                error_code=hold.error_code or CheckoutErrorCode.SEAT_CONFLICT,
            )
        if hold.seat_ids != request.seat_ids:
            return ProviderCheckoutObservation(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.PARTIAL_RESULT,
                seat_ids=hold.seat_ids,
            )
        await recorder.record(request.intent_id, CheckoutStage.HOLD_CREATED)

        form = await self._driver.open_contact_form()
        if form.auth_expired:
            return ProviderCheckoutObservation(
                CheckoutState.REJECTED,
                error_code=CheckoutErrorCode.AUTH_EXPIRED,
            )
        if form.captcha_present:
            return ProviderCheckoutObservation(
                CheckoutState.REQUIRES_USER_ACTION,
                error_code=CheckoutErrorCode.CAPTCHA,
            )
        if not _CONTACT_FIELDS.issubset(form.input_names) or not form.submitter_ready:
            return ProviderCheckoutObservation(
                CheckoutState.REQUIRES_USER_ACTION,
                error_code=CheckoutErrorCode.CONTRACT_CHANGED,
            )
        await recorder.record(request.intent_id, CheckoutStage.CONTACT_FORM_RECEIVED)
        await recorder.record(request.intent_id, CheckoutStage.CONFIRM_STARTED)
        return await self._driver.submit_contact_form(request)


class QuickTicketsCheckoutAdapter:
    """Turn one transport observation into a validated provider-neutral result."""

    def __init__(
        self,
        *,
        booking_mode: BookingMode,
        transport: CheckoutWriteTransport,
        recorder: CheckoutStageRecorder,
        payment_host: str = "quicktickets.ru",
    ) -> None:
        self._booking_mode = booking_mode
        self._transport = transport
        self._recorder = recorder
        self._payment_host = payment_host.casefold()

    async def submit(self, request: CheckoutRequest) -> CheckoutResult:
        """Execute at most one changing workflow and validate every success field."""
        if self._booking_mode is BookingMode.DRY_RUN:
            return CheckoutResult(CheckoutState.DRY_RUN)

        await self._recorder.record(request.intent_id, CheckoutStage.WRITE_STARTED)
        try:
            observation = await self._transport.submit(request, recorder=self._recorder)
        except TimeoutError:
            await self._recorder.record(
                request.intent_id,
                CheckoutStage.AMBIGUOUS,
                CheckoutErrorCode.TRANSPORT_TIMEOUT,
            )
            return CheckoutResult(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.TRANSPORT_TIMEOUT,
            )

        await self._recorder.record(request.intent_id, CheckoutStage.RESPONSE_RECEIVED)
        if observation.state is CheckoutState.REJECTED:
            error_code = observation.error_code or CheckoutErrorCode.CONTRACT_CHANGED
            await self._recorder.record(request.intent_id, CheckoutStage.REJECTED, error_code)
            return CheckoutResult(
                CheckoutState.REJECTED,
                error_code=error_code,
            )
        if observation.state is CheckoutState.REQUIRES_USER_ACTION:
            error_code = observation.error_code or CheckoutErrorCode.UNSUPPORTED
            await self._recorder.record(
                request.intent_id,
                CheckoutStage.REQUIRES_USER_ACTION,
                error_code,
            )
            return CheckoutResult(
                CheckoutState.REQUIRES_USER_ACTION,
                error_code=error_code,
            )
        if observation.state is not CheckoutState.CONFIRMED:
            error_code = observation.error_code or CheckoutErrorCode.PARTIAL_RESULT
            await self._recorder.record(request.intent_id, CheckoutStage.AMBIGUOUS, error_code)
            return CheckoutResult(
                CheckoutState.AMBIGUOUS,
                error_code=error_code,
            )

        result = self._validate_confirmed(request, observation)
        stage = (
            CheckoutStage.VALIDATED
            if result.state is CheckoutState.CONFIRMED
            else CheckoutStage.AMBIGUOUS
        )
        await self._recorder.record(request.intent_id, stage, result.error_code)
        return result

    def _validate_confirmed(
        self, request: CheckoutRequest, observation: ProviderCheckoutObservation
    ) -> CheckoutResult:
        if tuple(observation.seat_ids) != request.seat_ids:
            code = (
                CheckoutErrorCode.PARTIAL_RESULT
                if set(observation.seat_ids).issubset(request.seat_ids)
                else CheckoutErrorCode.SEAT_MISMATCH
            )
            return CheckoutResult(CheckoutState.AMBIGUOUS, error_code=code)
        if observation.total != request.expected_total:
            return CheckoutResult(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.AMOUNT_MISMATCH,
            )
        if observation.total is None or observation.total > request.reserved_total:
            return CheckoutResult(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.AMOUNT_MISMATCH,
            )
        if not observation.provider_order_id or not observation.provider_order_id.strip():
            return CheckoutResult(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.PARTIAL_RESULT,
            )
        if not observation.payment_url:
            return CheckoutResult(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.PAYMENT_URL_MISSING,
            )
        if not observation.payment_url_transferable or not self._valid_payment_url(
            observation.payment_url
        ):
            return CheckoutResult(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.INVALID_PAYMENT_URL,
            )
        if observation.held_at is None or observation.expires_at is None:
            return CheckoutResult(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.EXPIRY_MISSING,
            )
        if not _aware(observation.held_at) or not _aware(observation.expires_at):
            return CheckoutResult(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.CONTRACT_CHANGED,
            )
        if observation.expires_at <= observation.held_at:
            return CheckoutResult(
                CheckoutState.AMBIGUOUS,
                error_code=CheckoutErrorCode.CONTRACT_CHANGED,
            )

        order = ConfirmedOrder(
            provider_order_id=observation.provider_order_id.strip(),
            seat_ids=observation.seat_ids,
            total=observation.total,
            payment_url=observation.payment_url,
            held_at=observation.held_at,
            expires_at=observation.expires_at,
        )
        return CheckoutResult(CheckoutState.CONFIRMED, order=order)

    def _valid_payment_url(self, value: str) -> bool:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.hostname.casefold() == self._payment_host
            and parsed.username is None
            and parsed.password is None
            and _PAYMENT_PATH.fullmatch(parsed.path)
            and not parsed.fragment
        )


class MemoryCheckoutStageRecorder:
    """Small fake recorder for contract tests and dry-run composition."""

    def __init__(self) -> None:
        self.records: list[tuple[str, CheckoutStage]] = []

    async def record(
        self,
        intent_id: str,
        stage: CheckoutStage,
        error_code: CheckoutErrorCode | None = None,
    ) -> None:
        self.records.append((intent_id, stage))


class FakeCheckoutTransport:
    """Deterministic fake covering provider results without external writes."""

    def __init__(
        self,
        results: Iterable[ProviderCheckoutObservation | BaseException],
        *,
        intermediate_stages: tuple[CheckoutStage, ...] = (),
    ) -> None:
        self._results = deque(results)
        self._intermediate_stages = intermediate_stages
        self.calls: list[CheckoutRequest] = []

    async def submit(
        self, request: CheckoutRequest, *, recorder: CheckoutStageRecorder
    ) -> ProviderCheckoutObservation:
        self.calls.append(request)
        for stage in self._intermediate_stages:
            await recorder.record(request.intent_id, stage)
        if not self._results:
            raise AssertionError("fake checkout transport has no configured result")
        result = self._results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
