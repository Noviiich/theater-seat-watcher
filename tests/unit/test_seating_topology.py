from __future__ import annotations

from decimal import Decimal

import pytest

from theater_tickets.adapters.quicktickets.profiles import (
    DirectorySeatProfileSource,
    load_seat_selection_configuration,
    parse_hall_profile,
)
from theater_tickets.domain.errors import DomainValidationError
from theater_tickets.domain.models import Money, Seat, SeatAvailability
from theater_tickets.domain.seating.topology import infer_conservative_profile, topology_fingerprint


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


def test_inferred_topology_splits_a_large_gap_as_a_possible_aisle() -> None:
    profile = infer_conservative_profile(
        _seats()
        + (
            Seat(
                "d",
                "16",
                "Партер",
                "1",
                "0",
                Money(70000),
                SeatAvailability.FREE,
                x=100,
                y=0,
                width=1,
                height=1,
                rotation=0,
            ),
        )
    )

    assert [segment.seat_ids for segment in profile.row_segments] == [("a", "b", "c"), ("d",)]


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


def test_runtime_profile_loads_exact_decimal_selection_and_blocks_path_traversal(
    tmp_path,
) -> None:
    profile_path = tmp_path / "hall-16-v1.yaml"
    profile_path.write_text(
        """
profile_id: hall-16-v1
hall_id: "16"
topology_fingerprint: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
mode: automatic
row_segments:
  - id: main
    block: stalls
    row: "1"
    seat_ids: ["1", "2"]
preferred_groups: []
selection:
  row_quality: {"1": "1.00"}
  weights: {row: "0.40", center: "0.35", price: "0.15", aisle: "0.10"}
  min_quality: "0.60"
  view_axis_x: "100"
  normalization_width: "100"
  aisle_quality: {main: "0.80"}
""".strip(),
        encoding="utf-8",
    )
    configuration = load_seat_selection_configuration(profile_path)
    assert configuration.preferences.min_quality == Decimal("0.60")
    assert DirectorySeatProfileSource(tmp_path).load("hall-16-v1") == configuration
    with pytest.raises(LookupError, match="invalid"):
        DirectorySeatProfileSource(tmp_path).load("../private")
