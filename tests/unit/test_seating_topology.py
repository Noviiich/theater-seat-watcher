from __future__ import annotations

import pytest

from theater_tickets.adapters.quicktickets.profiles import parse_hall_profile
from theater_tickets.domain.errors import DomainValidationError
from theater_tickets.domain.models import Money, Seat, SeatAvailability
from theater_tickets.domain.seating.topology import topology_fingerprint


def _seats() -> tuple[Seat, ...]:
    return (
        Seat(
            "a",
            "16",
            "Партер",
            "1",
            "3",
            Money(70000),
            SeatAvailability.FREE,
            x=10,
            y=0,
            width=1,
            height=1,
            rotation=0,
        ),
        Seat(
            "b",
            "16",
            "Партер",
            "1",
            "2",
            Money(70000),
            SeatAvailability.SOLD,
            x=20,
            y=0,
            width=1,
            height=1,
            rotation=0,
        ),
        Seat(
            "c",
            "16",
            "Партер",
            "1",
            "1",
            Money(90000),
            SeatAvailability.FREE,
            x=30,
            y=0,
            width=1,
            height=1,
            rotation=0,
        ),
    )


def _raw() -> dict[str, object]:
    return {
        "profile_id": "hall-16-v1",
        "hall_id": "16",
        "topology_fingerprint": topology_fingerprint(_seats()),
        "mode": "manual_then_auto",
        "row_segments": [
            {"id": "stalls-1", "block": "Партер", "row": "1", "seat_ids": ["a", "b", "c"]}
        ],
        "preferred_groups": [{"id": "pair", "priority": 0, "seat_ids": ["a", "b"]}],
    }


def test_verified_profile_uses_explicit_order_and_ignores_price_availability_changes() -> None:
    profile = parse_hall_profile(_raw())
    changed = tuple(
        Seat(
            seat.provider_id,
            seat.hall_id,
            seat.block,
            seat.row_label,
            seat.seat_label,
            Money(seat.price.minor_units + 1),
            SeatAvailability.UNKNOWN,
            x=seat.x,
            y=seat.y,
            width=seat.width,
            height=seat.height,
            rotation=seat.rotation,
        )
        for seat in _seats()
    )

    assert profile.matches_inventory(_seats())
    assert profile.matches_inventory(changed)


def test_profile_rejects_passage_crossing_duplicate_and_unknown_seats() -> None:
    crossing = _raw()
    crossing["preferred_groups"] = [{"id": "bad", "priority": 0, "seat_ids": ["a", "c"]}]
    with pytest.raises(DomainValidationError, match="contiguous"):
        parse_hall_profile(crossing)

    duplicate = _raw()
    duplicate["row_segments"] = [
        {"id": "one", "block": "Партер", "row": "1", "seat_ids": ["a", "b"]},
        {"id": "two", "block": "Партер", "row": "1", "seat_ids": ["b", "c"]},
    ]
    with pytest.raises(DomainValidationError, match="multiple row segments"):
        parse_hall_profile(duplicate)

    unknown = _raw()
    unknown["preferred_groups"] = [{"id": "bad", "priority": 0, "seat_ids": ["a", "missing"]}]
    with pytest.raises(DomainValidationError, match="outside row segments"):
        parse_hall_profile(unknown)


def test_profile_detects_geometry_and_row_mismatch() -> None:
    profile = parse_hall_profile(_raw())
    shifted = list(_seats())
    shifted[0] = Seat(
        "a",
        "16",
        "Партер",
        "1",
        "3",
        Money(70000),
        SeatAvailability.FREE,
        x=11,
        y=0,
        width=1,
        height=1,
        rotation=0,
    )
    assert not profile.matches_inventory(tuple(shifted))

    wrong_row = list(_seats())
    wrong_row[0] = Seat(
        "a",
        "16",
        "Партер",
        "2",
        "3",
        Money(70000),
        SeatAvailability.FREE,
        x=10,
        y=0,
        width=1,
        height=1,
        rotation=0,
    )
    assert not profile.matches_inventory(tuple(wrong_row))
