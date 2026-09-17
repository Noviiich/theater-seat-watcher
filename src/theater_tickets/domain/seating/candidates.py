"""Pure generation of truly adjacent groups from explicit topology segments."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from theater_tickets.domain.errors import DomainValidationError
from theater_tickets.domain.models import Money, Seat, SeatAvailability, SeatGroup
from theater_tickets.domain.seating.topology import HallProfile, SeatingMode


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    row: Decimal
    center: Decimal
    price: Decimal
    aisle: Decimal

    def __post_init__(self) -> None:
        values = (self.row, self.center, self.price, self.aisle)
        if any(value < 0 for value in values) or sum(values) != Decimal("1"):
            raise DomainValidationError("scoring weights must be non-negative and total one")


@dataclass(frozen=True, slots=True)
class SelectionPreferences:
    """Selection constraints supplied from a validated subscription and hall profile."""

    ticket_count: int
    max_ticket_price: Money | None
    max_order_total: Money | None
    row_quality: dict[str, Decimal]
    weights: ScoringWeights
    min_quality: Decimal
    view_axis_x: Decimal
    normalization_width: Decimal
    aisle_quality: dict[str, Decimal]

    def __post_init__(self) -> None:
        if self.ticket_count <= 0:
            raise DomainValidationError("ticket_count must be positive")
        if not Decimal("0") <= self.min_quality <= Decimal("1"):
            raise DomainValidationError("min_quality must be between zero and one")
        if self.normalization_width <= 0:
            raise DomainValidationError("normalization_width must be positive")
        for values in (self.row_quality.values(), self.aisle_quality.values()):
            if any(value < 0 or value > 1 for value in values):
                raise DomainValidationError("quality components must be between zero and one")


@dataclass(frozen=True, slots=True)
class RankedGroup:
    group: SeatGroup
    source: str
    manual_priority: int | None
    physical_start_index: int
    explanation: str


@dataclass(frozen=True, slots=True)
class RankingResult:
    groups: tuple[RankedGroup, ...]
    reason: str | None = None


def rank_groups(
    inventory: tuple[Seat, ...], profile: HallProfile, preferences: SelectionPreferences
) -> RankingResult:
    """Return only affordable continuous groups, never arbitrary seat combinations."""
    if not profile.matches_inventory(inventory):
        return RankingResult((), "profile_mismatch")
    seats_by_id = {seat.provider_id: seat for seat in inventory}
    manual = _manual_groups(seats_by_id, profile, preferences)
    automatic = _automatic_groups(seats_by_id, profile, preferences)
    if profile.mode is SeatingMode.MANUAL_ONLY:
        candidates = manual
    elif profile.mode is SeatingMode.AUTOMATIC:
        candidates = automatic
    else:
        candidates = manual if manual else automatic
    if not candidates:
        return RankingResult((), "no_adjacent_group")
    return RankingResult(tuple(sorted(candidates, key=_sort_key)))


def _manual_groups(
    seats_by_id: dict[str, Seat], profile: HallProfile, preferences: SelectionPreferences
) -> list[RankedGroup]:
    segment_by_seat = {
        seat_id: segment for segment in profile.row_segments for seat_id in segment.seat_ids
    }
    candidates: list[RankedGroup] = []
    for group in profile.preferred_groups:
        if len(group.seat_ids) != preferences.ticket_count:
            continue
        segment = segment_by_seat[group.seat_ids[0]]
        seats = tuple(seats_by_id[seat_id] for seat_id in group.seat_ids)
        candidate = _rank(
            seats, segment.segment_id, segment.seat_ids.index(group.seat_ids[0]), preferences
        )
        if candidate is not None:
            candidates.append(
                RankedGroup(
                    candidate.group,
                    "manual",
                    group.priority,
                    candidate.physical_start_index,
                    candidate.explanation,
                )
            )
    return candidates


def _automatic_groups(
    seats_by_id: dict[str, Seat], profile: HallProfile, preferences: SelectionPreferences
) -> list[RankedGroup]:
    candidates: list[RankedGroup] = []
    count = preferences.ticket_count
    for segment in profile.row_segments:
        for start in range(len(segment.seat_ids) - count + 1):
            seats = tuple(
                seats_by_id[seat_id] for seat_id in segment.seat_ids[start : start + count]
            )
            candidate = _rank(seats, segment.segment_id, start, preferences)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _rank(
    seats: tuple[Seat, ...], segment_id: str, start: int, preferences: SelectionPreferences
) -> RankedGroup | None:
    if any(seat.availability is not SeatAvailability.FREE for seat in seats):
        return None
    if preferences.max_ticket_price and any(
        seat.price > preferences.max_ticket_price for seat in seats
    ):
        return None
    total = sum((seat.price for seat in seats), start=Money(0))
    if preferences.max_order_total and total > preferences.max_order_total:
        return None
    row = preferences.row_quality.get(seats[0].row_label)
    aisle = preferences.aisle_quality.get(segment_id)
    if row is None or aisle is None or any(seat.x is None for seat in seats):
        return None
    assert seats[0].x is not None and seats[-1].x is not None
    center = (Decimal(seats[0].x) + Decimal(seats[-1].x)) / Decimal(2)
    center_quality = max(
        Decimal(0),
        Decimal(1) - abs(center - preferences.view_axis_x) / preferences.normalization_width,
    )
    price_quality = (
        max(
            Decimal(0),
            Decimal(1)
            - Decimal(total.minor_units) / Decimal(preferences.max_order_total.minor_units),
        )
        if preferences.max_order_total and preferences.max_order_total.minor_units > 0
        else Decimal(1)
    )
    quality = (
        preferences.weights.row * row
        + preferences.weights.center * center_quality
        + preferences.weights.price * price_quality
        + preferences.weights.aisle * aisle
    )
    if quality < preferences.min_quality:
        return None
    group = SeatGroup(segment_id=segment_id, seats=seats, quality=quality)
    labels = ", ".join(seat.seat_label for seat in seats)
    amount = Decimal(total.minor_units) / Decimal(100)
    return RankedGroup(
        group,
        "automatic",
        None,
        start,
        f"Ряд {seats[0].row_label}, места {labels}; {amount:.2f} ₽",
    )


def _sort_key(value: RankedGroup) -> tuple[int, int, Decimal, int, str, int, tuple[str, ...]]:
    return (
        0 if value.source == "manual" else 1,
        value.manual_priority if value.manual_priority is not None else 0,
        -value.group.quality,
        value.group.total.minor_units,
        value.group.segment_id,
        value.physical_start_index,
        tuple(seat.provider_id for seat in value.group.seats),
    )
