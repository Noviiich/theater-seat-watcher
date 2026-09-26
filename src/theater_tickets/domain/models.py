"""I/O-free value objects and validated configuration for the booking domain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Self

from theater_tickets.domain.errors import DomainValidationError


def _non_empty_identifier(value: str | int, *, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise DomainValidationError(f"{name} must not be empty")
    return normalized


class BookingMode(StrEnum):
    """Whether a subscription can create a real provider hold."""

    DRY_RUN = "dry_run"
    LIVE = "live"


class SeatAvailability(StrEnum):
    """Availability after combining the hall and fresh inventory responses."""

    FREE = "free"
    SOLD = "sold"
    HELD = "held"
    DISABLED = "disabled"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SaleCapabilities:
    """Per-session provider limits; collective sale is deliberately separate."""

    sell_available: bool
    sell_max: int
    book_available: bool
    book_max: int
    collective_sell_available: bool
    collective_sell_min: int | None
    collective_sell_max: int | None

    def __post_init__(self) -> None:
        if self.sell_max < 0 or self.book_max < 0:
            raise DomainValidationError("sale maximums must not be negative")
        for name in ("collective_sell_min", "collective_sell_max"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise DomainValidationError(f"{name} must be positive when set")
        if (
            self.collective_sell_min is not None
            and self.collective_sell_max is not None
            and self.collective_sell_min > self.collective_sell_max
        ):
            raise DomainValidationError("collective sale minimum must not exceed maximum")

    def allows_regular_sale(self, ticket_count: int) -> bool:
        """Only ordinary sell limits can authorize the booking flow."""
        return ticket_count > 0 and self.sell_available and ticket_count <= self.sell_max


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """A non-negative money amount stored as integer minor units."""

    minor_units: int
    currency: str = "RUB"

    def __post_init__(self) -> None:
        if self.minor_units < 0:
            raise DomainValidationError("minor_units must not be negative")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise DomainValidationError("currency must be a three-letter code")

    @classmethod
    def from_rubles(cls, value: Decimal | str | int) -> Self:
        """Convert a provider ruble value exactly, rejecting float imprecision."""
        if isinstance(value, float):
            raise DomainValidationError("float prices are not allowed")
        try:
            rubles = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise DomainValidationError("price must be a decimal ruble value") from exc
        minor_units = rubles * Decimal(100)
        integral = minor_units.to_integral_value()
        if minor_units != integral:
            raise DomainValidationError("price must not contain fractions of a kopeck")
        return cls(minor_units=int(integral))

    def __add__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise DomainValidationError("cannot add different currencies")
        return Money(self.minor_units + other.minor_units, self.currency)


@dataclass(frozen=True, slots=True)
class SessionKey:
    """A provider session identity scoped by theatre alias."""

    provider: str
    theatre_alias: str
    session_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _non_empty_identifier(self.provider, name="provider"))
        object.__setattr__(
            self,
            "theatre_alias",
            _non_empty_identifier(self.theatre_alias, name="theatre_alias"),
        )
        object.__setattr__(
            self, "session_id", _non_empty_identifier(self.session_id, name="session_id")
        )


@dataclass(frozen=True, slots=True)
class Session:
    """A concrete dated performance, distinct from its event."""

    key: SessionKey
    event_id: str
    hall_id: str
    title: str
    starts_at: datetime

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None or self.starts_at.utcoffset() is None:
            raise DomainValidationError("starts_at must be timezone-aware")
        object.__setattr__(self, "event_id", _non_empty_identifier(self.event_id, name="event_id"))
        object.__setattr__(self, "hall_id", _non_empty_identifier(self.hall_id, name="hall_id"))
        object.__setattr__(self, "title", _non_empty_identifier(self.title, name="title"))
        object.__setattr__(self, "starts_at", self.starts_at.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class Seat:
    """One provider place; blank row and number together mean unnumbered entry."""

    provider_id: str
    hall_id: str
    block: str
    row_label: str
    seat_label: str
    price: Money
    availability: SeatAvailability
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None
    rotation: int | None = None

    def __post_init__(self) -> None:
        for name in ("provider_id", "hall_id", "block"):
            object.__setattr__(self, name, _non_empty_identifier(getattr(self, name), name=name))
        if not isinstance(self.row_label, str) or not isinstance(self.seat_label, str):
            raise DomainValidationError("place row and number must be strings")
        row, number = self.row_label.strip(), self.seat_label.strip()
        if bool(row) != bool(number):
            raise DomainValidationError("place row and number must both be present or blank")
        object.__setattr__(self, "row_label", row)
        object.__setattr__(self, "seat_label", number)
        for name in ("x", "y", "width", "height", "rotation"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, int):
                raise DomainValidationError(f"{name} must be an integer when set")

    @property
    def is_unnumbered(self) -> bool:
        return not self.row_label


@dataclass(frozen=True, slots=True)
class SeatGroup:
    """A verified, continuous sequence of seats within one row segment."""

    segment_id: str
    seats: tuple[Seat, ...]
    quality: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "segment_id", _non_empty_identifier(self.segment_id, name="segment_id")
        )
        if not self.seats:
            raise DomainValidationError("seat group must contain at least one seat")
        if len({seat.provider_id for seat in self.seats}) != len(self.seats):
            raise DomainValidationError("seat group must not contain duplicate seats")
        if any(seat.availability is not SeatAvailability.FREE for seat in self.seats):
            raise DomainValidationError("seat group must contain only free seats")
        if len(self.seats) > 1 and any(seat.is_unnumbered for seat in self.seats):
            raise DomainValidationError("unnumbered places cannot form an adjacent group")
        if not Decimal("0") <= self.quality <= Decimal("1"):
            raise DomainValidationError("quality must be between zero and one")

    @property
    def total(self) -> Money:
        return sum((seat.price for seat in self.seats), start=Money(0))


@dataclass(frozen=True, slots=True)
class RenewalPolicy:
    """Timing constraints for one candidate's consecutive booking cycles."""

    renewal_interval_seconds: int = 1200
    expected_hold_ttl_seconds: int = 1200
    availability_retry_seconds: int = 180
    max_cycles_per_session: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "renewal_interval_seconds",
            "expected_hold_ttl_seconds",
            "availability_retry_seconds",
        ):
            if getattr(self, name) <= 0:
                raise DomainValidationError(f"{name} must be positive")
        if self.max_cycles_per_session is not None and self.max_cycles_per_session <= 0:
            raise DomainValidationError("max_cycles_per_session must be positive when set")


@dataclass(frozen=True, slots=True)
class Subscription:
    """Validated purchase criteria for one buyer's monitoring rule."""

    subscription_id: str
    buyer_id: str
    theatre_alias: str
    ticket_count: int
    seat_profile_id: str
    max_sessions_per_batch: int | None
    max_ticket_price: Money | None = None
    max_order_total: Money | None = None
    max_batch_total: Money | None = None
    max_active_orders: int | None = 1
    max_active_total: Money | None = None
    priority: int = 0
    enabled: bool = True
    booking_mode: BookingMode = BookingMode.DRY_RUN
    event_ids: frozenset[str] = field(default_factory=frozenset)
    title_filter: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    weekdays: frozenset[int] = field(default_factory=frozenset)
    time_from: time | None = None
    time_to: time | None = None
    renewal_policy: RenewalPolicy = field(default_factory=RenewalPolicy)
    individual_orders: bool = False

    def __post_init__(self) -> None:
        for name in ("subscription_id", "buyer_id", "theatre_alias", "seat_profile_id"):
            object.__setattr__(self, name, _non_empty_identifier(getattr(self, name), name=name))
        if self.ticket_count <= 0:
            raise DomainValidationError("ticket_count must be positive")
        if (
            self.individual_orders
            and self.max_order_total is None
            and (self.max_batch_total is not None or self.max_active_total is not None)
        ):
            raise DomainValidationError("aggregate budgets require an order limit for fees")
        if self.max_sessions_per_batch is not None and self.max_sessions_per_batch <= 0:
            raise DomainValidationError("max_sessions_per_batch must be positive")
        if self.max_active_orders is not None and self.max_active_orders <= 0:
            raise DomainValidationError("max_active_orders must be positive")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise DomainValidationError("date_from must not be after date_to")
        if self.time_from and self.time_to and self.time_from > self.time_to:
            raise DomainValidationError("time_from must not be after time_to")
        if any(day < 0 or day > 6 for day in self.weekdays):
            raise DomainValidationError(
                "weekdays must contain ISO weekday indexes from zero to six"
            )
        normalized_event_ids = frozenset(
            _non_empty_identifier(value, name="event_id") for value in self.event_ids
        )
        object.__setattr__(self, "event_ids", normalized_event_ids)
        if self.title_filter is not None:
            title_filter = self.title_filter.strip()
            if not title_filter:
                raise DomainValidationError("title_filter must not be blank")
            object.__setattr__(self, "title_filter", title_filter.casefold())

    def matches(self, session: Session, *, local_starts_at: datetime) -> bool:
        """Match filters using an explicitly supplied Europe/Moscow projection."""
        if local_starts_at.tzinfo is None or local_starts_at.utcoffset() is None:
            raise DomainValidationError("local_starts_at must be timezone-aware")
        if session.key.theatre_alias != self.theatre_alias:
            return False
        if self.event_ids and session.event_id not in self.event_ids:
            return False
        if self.title_filter and self.title_filter not in session.title.casefold():
            return False
        local_date = local_starts_at.date()
        local_time = local_starts_at.timetz().replace(tzinfo=None)
        if self.date_from and local_date < self.date_from:
            return False
        if self.date_to and local_date > self.date_to:
            return False
        if self.weekdays and local_starts_at.weekday() not in self.weekdays:
            return False
        if self.time_from and local_time < self.time_from:
            return False
        return not self.time_to or local_time <= self.time_to

    def allows_seat_group(self, group: SeatGroup) -> bool:
        """Apply the known pre-check limits before a provider checkout quote."""
        if len(group.seats) != self.order_ticket_count:
            return False
        if self.max_ticket_price and any(
            seat.price > self.max_ticket_price for seat in group.seats
        ):
            return False
        return not self.max_order_total or group.total <= self.max_order_total

    @property
    def order_ticket_count(self) -> int:
        return 1 if self.individual_orders else self.ticket_count

    @property
    def price_unlimited(self) -> bool:
        return self.individual_orders and self.max_order_total is None
