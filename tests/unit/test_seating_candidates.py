from __future__ import annotations

from decimal import Decimal

from theater_tickets.domain.models import Money, Seat, SeatAvailability
from theater_tickets.domain.seating.candidates import (
    ScoringWeights,
    SelectionPreferences,
    rank_groups,
)
from theater_tickets.domain.seating.topology import (
    HallProfile,
    PreferredGroup,
    RowSegment,
    SeatingMode,
    topology_fingerprint,
)


def _inventory(states: tuple[SeatAvailability, ...] | None = None) -> tuple[Seat, ...]:
    states = states or (SeatAvailability.FREE,) * 5
    return tuple(
        Seat(
            str(index),
            "hall",
            "Партер",
            "6",
            str(index),
            Money(10000 + index * 1000),
            state,
            x=index * 10,
            y=0,
        )
        for index, state in enumerate(states, 1)
    )


def _profile(inventory: tuple[Seat, ...], mode: SeatingMode = SeatingMode.AUTOMATIC) -> HallProfile:
    return HallProfile(
        "profile",
        "hall",
        topology_fingerprint(inventory),
        mode,
        (
            RowSegment("left", "Партер", "6", ("1", "2", "3")),
            RowSegment("right", "Партер", "6", ("4", "5")),
        ),
        (PreferredGroup("manual", 1, ("2", "3")),),
    )


def _preferences(**overrides: object) -> SelectionPreferences:
    values: dict[str, object] = {
        "ticket_count": 2,
        "max_ticket_price": None,
        "max_order_total": Money(30000),
        "row_quality": {"6": Decimal("1")},
        "weights": ScoringWeights(Decimal("0.4"), Decimal("0.3"), Decimal("0.2"), Decimal("0.1")),
        "min_quality": Decimal("0"),
        "view_axis_x": Decimal("25"),
        "normalization_width": Decimal("30"),
        "aisle_quality": {"left": Decimal("1"), "right": Decimal("1")},
    }
    values.update(overrides)
    return SelectionPreferences(**values)  # type: ignore[arg-type]


def test_automatic_windows_never_cross_passages_or_occupied_seats() -> None:
    inventory = _inventory(
        (
            SeatAvailability.FREE,
            SeatAvailability.SOLD,
            SeatAvailability.FREE,
            SeatAvailability.FREE,
            SeatAvailability.FREE,
        )
    )
    result = rank_groups(inventory, _profile(inventory), _preferences())

    assert [tuple(seat.provider_id for seat in item.group.seats) for item in result.groups] == [
        ("4", "5")
    ]


def test_manual_then_auto_prefers_eligible_manual_group_and_has_stable_tie_breaker() -> None:
    inventory = _inventory()
    profile = _profile(inventory, SeatingMode.MANUAL_THEN_AUTO)
    result = rank_groups(inventory, profile, _preferences(view_axis_x=Decimal("25")))

    assert result.groups[0].source == "manual"
    assert tuple(seat.provider_id for seat in result.groups[0].group.seats) == ("2", "3")

    unavailable_manual = _inventory(
        (
            SeatAvailability.FREE,
            SeatAvailability.SOLD,
            SeatAvailability.FREE,
            SeatAvailability.FREE,
            SeatAvailability.FREE,
        )
    )
    fallback = rank_groups(
        unavailable_manual,
        _profile(unavailable_manual, SeatingMode.MANUAL_THEN_AUTO),
        _preferences(),
    )
    assert tuple(seat.provider_id for seat in fallback.groups[0].group.seats) == ("4", "5")


def test_budget_unknown_profile_and_mismatch_are_explicitly_rejected() -> None:
    inventory = _inventory()
    profile = _profile(inventory)
    no_budget = rank_groups(inventory, profile, _preferences(max_order_total=Money(10000)))
    assert no_budget.reason == "no_adjacent_group"

    no_row_score = rank_groups(inventory, profile, _preferences(row_quality={}))
    assert no_row_score.reason == "no_adjacent_group"

    changed = list(inventory)
    changed[0] = Seat(
        "1", "hall", "Партер", "6", "1", Money(11000), SeatAvailability.FREE, x=999, y=0
    )
    mismatch = rank_groups(tuple(changed), profile, _preferences())
    assert mismatch.reason == "profile_mismatch"
