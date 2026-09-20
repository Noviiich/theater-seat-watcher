"""Startup recovery for interrupted changing checkout operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from theater_tickets.application.checkout import (
    CheckoutErrorCode,
    CheckoutObservationValidator,
    CheckoutRequest,
    CheckoutResult,
    CheckoutState,
    ProviderCheckoutObservation,
)


class ReconciliationState(StrEnum):
    """What a provider can prove about one earlier changing attempt."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


class RecoveryDisposition(StrEnum):
    """Durable local action after startup recovery."""

    RETRY_ALLOWED = "retry_allowed"
    CONFIRMED = "confirmed"
    NEEDS_ATTENTION = "needs_attention"


class ManualResolution(StrEnum):
    """Operator decisions that do not invent a successful provider order."""

    ORDER_AND_HOLD_ABSENT = "order_and_hold_absent"
    KEEP_BLOCKED = "keep_blocked"


@dataclass(frozen=True, slots=True, repr=False)
class ReconciliationObservation:
    state: ReconciliationState
    checkout: ProviderCheckoutObservation | None = None

    def __post_init__(self) -> None:
        if (self.state is ReconciliationState.FOUND) != (self.checkout is not None):
            raise ValueError("only found reconciliation may contain checkout data")


@dataclass(frozen=True, slots=True)
class IncompleteCheckout:
    intent_id: str
    write_started: bool


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryOutcome:
    intent_id: str
    disposition: RecoveryDisposition
    checkout_result: CheckoutResult | None = None
    error_code: CheckoutErrorCode | None = None


class CheckoutReconciliationTransport(Protocol):
    """Lookup an earlier write without checking payment or cancelling it."""

    async def reconcile(self, request: CheckoutRequest) -> ReconciliationObservation:
        """Return found/not-found/unknown/unsupported without creating an order."""


class RecoveryRepository(Protocol):
    async def list_incomplete(self) -> tuple[IncompleteCheckout, ...]:
        """Return incomplete intents in deterministic order."""

    async def apply(self, outcome: RecoveryOutcome) -> None:
        """Persist one recovery decision in a short transaction."""


class RecoveryRequestLoader(Protocol):
    async def load(self, intent_id: str) -> CheckoutRequest:
        """Rebuild the exact request and private buyer context for reconciliation."""


class CheckoutRecoveryService:
    """Recover incomplete intents without ever resubmitting checkout."""

    def __init__(
        self,
        *,
        repository: RecoveryRepository,
        request_loader: RecoveryRequestLoader,
        transport: CheckoutReconciliationTransport,
        validator: CheckoutObservationValidator,
    ) -> None:
        self._repository = repository
        self._request_loader = request_loader
        self._transport = transport
        self._validator = validator

    async def recover_startup(self) -> tuple[RecoveryOutcome, ...]:
        outcomes: list[RecoveryOutcome] = []
        for incomplete in await self._repository.list_incomplete():
            outcome = await self._recover_one(incomplete)
            await self._repository.apply(outcome)
            outcomes.append(outcome)
        return tuple(outcomes)

    async def resolve_manually(
        self, intent_id: str, resolution: ManualResolution
    ) -> RecoveryOutcome:
        """Apply an explicit operator decision after unsupported reconciliation."""
        if resolution is ManualResolution.ORDER_AND_HOLD_ABSENT:
            outcome = RecoveryOutcome(intent_id, RecoveryDisposition.RETRY_ALLOWED)
        else:
            outcome = RecoveryOutcome(
                intent_id,
                RecoveryDisposition.NEEDS_ATTENTION,
                error_code=CheckoutErrorCode.UNSUPPORTED,
            )
        await self._repository.apply(outcome)
        return outcome

    async def _recover_one(self, incomplete: IncompleteCheckout) -> RecoveryOutcome:
        if not incomplete.write_started:
            return RecoveryOutcome(
                incomplete.intent_id,
                RecoveryDisposition.RETRY_ALLOWED,
            )

        request = await self._request_loader.load(incomplete.intent_id)
        try:
            observation = await self._transport.reconcile(request)
        except TimeoutError:
            return RecoveryOutcome(
                incomplete.intent_id,
                RecoveryDisposition.NEEDS_ATTENTION,
                error_code=CheckoutErrorCode.TRANSPORT_TIMEOUT,
            )
        if observation.state is ReconciliationState.NOT_FOUND:
            return RecoveryOutcome(
                incomplete.intent_id,
                RecoveryDisposition.RETRY_ALLOWED,
            )
        if observation.state is ReconciliationState.UNSUPPORTED:
            return RecoveryOutcome(
                incomplete.intent_id,
                RecoveryDisposition.NEEDS_ATTENTION,
                error_code=CheckoutErrorCode.UNSUPPORTED,
            )
        if observation.state is ReconciliationState.UNKNOWN:
            return RecoveryOutcome(
                incomplete.intent_id,
                RecoveryDisposition.NEEDS_ATTENTION,
                error_code=CheckoutErrorCode.PARTIAL_RESULT,
            )

        assert observation.checkout is not None
        result = self._validator.validate_observation(request, observation.checkout)
        if result.state is CheckoutState.CONFIRMED:
            return RecoveryOutcome(
                incomplete.intent_id,
                RecoveryDisposition.CONFIRMED,
                checkout_result=result,
            )
        return RecoveryOutcome(
            incomplete.intent_id,
            RecoveryDisposition.NEEDS_ATTENTION,
            checkout_result=result,
            error_code=result.error_code or CheckoutErrorCode.PARTIAL_RESULT,
        )
