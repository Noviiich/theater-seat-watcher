"""Provider-neutral checkout values and ports.

The application owns these contracts so provider adapters cannot leak HTML,
cookies, opaque order tokens, or transport-specific DTOs into the domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from theater_tickets.domain.errors import DomainValidationError
from theater_tickets.domain.models import Money, SessionKey


def _required(value: str, *, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise DomainValidationError(f"{name} must not be empty")
    return normalized


class CheckoutState(StrEnum):
    """A normalized outcome; only CONFIRMED contains a payable order."""

    DRY_RUN = "dry_run"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    REQUIRES_USER_ACTION = "requires_user_action"
    AMBIGUOUS = "ambiguous"


class CheckoutErrorCode(StrEnum):
    """Stable failure categories used by workers and reconciliation."""

    SEAT_CONFLICT = "seat_conflict"
    SEAT_MISMATCH = "seat_mismatch"
    AMOUNT_MISMATCH = "amount_mismatch"
    PARTIAL_RESULT = "partial_result"
    AUTH_EXPIRED = "auth_expired"
    CAPTCHA = "captcha"
    INVALID_PAYMENT_URL = "invalid_payment_url"
    PAYMENT_URL_MISSING = "payment_url_missing"
    EXPIRY_MISSING = "expiry_missing"
    CONTRACT_CHANGED = "contract_changed"
    TRANSPORT_TIMEOUT = "transport_timeout"
    UNSUPPORTED = "unsupported"


class CheckoutStage(StrEnum):
    """Durable milestones around changing provider operations."""

    WRITE_STARTED = "write_started"
    HOLD_CREATED = "hold_created"
    CONTACT_FORM_RECEIVED = "contact_form_received"
    CONFIRM_STARTED = "confirm_started"
    RESPONSE_RECEIVED = "response_received"
    VALIDATED = "validated"
    REJECTED = "rejected"
    REQUIRES_USER_ACTION = "requires_user_action"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True, repr=False)
class CheckoutBuyer:
    """Secret buyer values passed only to an explicitly enabled live transport."""

    lastname: str
    firstname: str
    middlename: str
    email: str
    phone: str
    personal_data_consent: bool

    def __post_init__(self) -> None:
        for name in ("lastname", "firstname", "middlename", "email", "phone"):
            object.__setattr__(self, name, _required(getattr(self, name), name=name))
        if not self.personal_data_consent:
            raise DomainValidationError("checkout requires personal data consent")


@dataclass(frozen=True, slots=True, repr=False)
class CheckoutRequest:
    """One already-persisted intent with an exact provider quote."""

    intent_id: str
    session_key: SessionKey
    seat_ids: tuple[str, ...]
    expected_total: Money
    reserved_total: Money
    expected_hold_ttl_seconds: int
    buyer: CheckoutBuyer

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _required(self.intent_id, name="intent_id"))
        normalized = tuple(_required(value, name="seat_id") for value in self.seat_ids)
        if not normalized or len(normalized) != len(set(normalized)):
            raise DomainValidationError("seat_ids must contain unique provider IDs")
        object.__setattr__(self, "seat_ids", normalized)
        if self.expected_total.currency != self.reserved_total.currency:
            raise DomainValidationError("checkout totals must use one currency")
        if self.expected_total.minor_units <= 0:
            raise DomainValidationError("expected_total must be positive")
        if self.expected_total > self.reserved_total:
            raise DomainValidationError("expected_total must not exceed reserved_total")
        if self.expected_hold_ttl_seconds <= 0:
            raise DomainValidationError("expected_hold_ttl_seconds must be positive")


@dataclass(frozen=True, slots=True, repr=False)
class ProviderCheckoutObservation:
    """Sanitized provider result; partial fields are allowed for failure handling."""

    state: CheckoutState
    error_code: CheckoutErrorCode | None = None
    provider_order_id: str | None = None
    seat_ids: tuple[str, ...] = ()
    total: Money | None = None
    payment_url: str | None = None
    held_at: datetime | None = None
    expires_at: datetime | None = None
    payment_url_transferable: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmedOrder:
    """Validated payable order. Its payment URL is deliberately repr-hidden."""

    provider_order_id: str
    seat_ids: tuple[str, ...]
    total: Money
    payment_url: str
    held_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class CheckoutResult:
    """Typed result returned by every checkout adapter invocation."""

    state: CheckoutState
    error_code: CheckoutErrorCode | None = None
    order: ConfirmedOrder | None = None

    def __post_init__(self) -> None:
        if (self.state is CheckoutState.CONFIRMED) != (self.order is not None):
            raise DomainValidationError("only a confirmed checkout may contain an order")


class CheckoutStageRecorder(Protocol):
    """Persists remote progress in short transactions outside provider I/O."""

    async def record(
        self,
        intent_id: str,
        stage: CheckoutStage,
        error_code: CheckoutErrorCode | None = None,
    ) -> None:
        """Record a sanitized stage without buyer or order secrets."""


class CheckoutWriteTransport(Protocol):
    """Potentially changing provider transport; it must never retry blindly."""

    async def submit(
        self, request: CheckoutRequest, *, recorder: CheckoutStageRecorder
    ) -> ProviderCheckoutObservation:
        """Execute the browser/HTTP workflow once for the persisted intent."""


class CheckoutObservationValidator(Protocol):
    """Validate a provider observation without issuing another write."""

    def validate_observation(
        self, request: CheckoutRequest, observation: ProviderCheckoutObservation
    ) -> CheckoutResult:
        """Return CONFIRMED only for a complete, exact payment handoff."""
