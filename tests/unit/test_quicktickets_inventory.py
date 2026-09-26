from __future__ import annotations

import pytest

from theater_tickets.adapters.quicktickets.errors import QuickTicketsContractError
from theater_tickets.adapters.quicktickets.inventory import parse_inventory, parse_sale_capabilities
from theater_tickets.domain.models import SeatAvailability


def _hall() -> dict[str, object]:
    return {
        "response": {
            "hall_id": 16,
            "places": [
                {
                    "id": 1,
                    "block": "Партер",
                    "series": "1",
                    "place": "13",
                    "status": "free",
                    "price": "700.50",
                    "x": 1,
                    "y": 2,
                    "width": 3,
                    "height": 4,
                    "rotate": 0,
                },
                {
                    "id": "2",
                    "block": "Партер",
                    "series": "1",
                    "place": "12",
                    "status": "disabled",
                    "price": 700,
                },
                {
                    "id": 3,
                    "block": "Балкон",
                    "series": "A",
                    "place": "1",
                    "status": "free",
                    "price": 900,
                },
            ],
        }
    }


def test_inventory_overlays_complete_availability_and_preserves_geometry() -> None:
    inventory = parse_inventory(
        _hall(), {"response": {"places": {"1": {"status": "sell"}, "3": {"status": "new"}}}}
    )

    assert inventory.hall_id == "16"
    assert [seat.availability for seat in inventory.seats] == [
        SeatAvailability.SOLD,
        SeatAvailability.DISABLED,
        SeatAvailability.UNKNOWN,
    ]
    assert inventory.seats[0].price.minor_units == 70050
    assert (inventory.seats[0].x, inventory.seats[0].width) == (1, 3)


def test_inventory_accepts_unnumbered_hall_place_but_rejects_partial_labels() -> None:
    hall = _hall()
    response = hall["response"]
    assert isinstance(response, dict)
    places = response["places"]
    assert isinstance(places, list)
    place = places[0]
    assert isinstance(place, dict)
    place.update(block="Входное место", series="", place="")

    inventory = parse_inventory(hall, {"response": {"places": {}}})
    assert inventory.seats[0].is_unnumbered
    assert inventory.seats[0].provider_id == "1"

    place["place"] = "1"
    with pytest.raises(QuickTicketsContractError, match="incomplete"):
        parse_inventory(hall, {"response": {"places": {}}})


def test_inventory_uses_verified_session_hall_id_when_live_response_omits_it() -> None:
    hall = _hall()
    response = hall["response"]
    assert isinstance(response, dict)
    response.pop("hall_id")

    inventory = parse_inventory(
        hall,
        {"response": {"places": {"1": {"status": "free"}, "3": {"status": "free"}}}},
        expected_hall_id="16",
    )

    assert inventory.hall_id == "16"
    with pytest.raises(QuickTicketsContractError, match="conflicts"):
        parse_inventory(_hall(), {"response": {"places": {}}}, expected_hall_id="other")


def test_inventory_accepts_an_id_keyed_place_mapping_only_when_ids_match() -> None:
    hall = _hall()
    response = hall["response"]
    assert isinstance(response, dict)
    places = response["places"]
    assert isinstance(places, list)
    response["places"] = {str(item["id"]): item for item in places if isinstance(item, dict)}

    inventory = parse_inventory(
        hall,
        {"response": {"places": {"1": {"status": "free"}, "3": {"status": "free"}}}},
    )

    assert len(inventory.seats) == 3
    response["places"] = {"wrong": places[0]}
    with pytest.raises(QuickTicketsContractError, match="invalid ID"):
        parse_inventory(hall, {"response": {"places": {}}})


def test_inventory_rejects_missing_hall_id_without_verified_session_details() -> None:
    hall = _hall()
    response = hall["response"]
    assert isinstance(response, dict)
    response.pop("hall_id")

    with pytest.raises(QuickTicketsContractError, match="absent"):
        parse_inventory(hall, {"response": {"places": {}}})


@pytest.mark.parametrize(
    "availability",
    [
        {"response": {}},
        {"response": {"places": []}},
        {"response": {"places": {"1": "sell"}}},
    ],
)
def test_inventory_rejects_incomplete_or_malformed_availability(
    availability: dict[str, object],
) -> None:
    with pytest.raises(QuickTicketsContractError):
        parse_inventory(_hall(), availability)


def test_sale_capabilities_use_only_the_requested_regular_sale_limit() -> None:
    payload = {
        "response": {
            "events": [
                {
                    "sessions": [
                        {
                            "id": 2,
                            "sell": {"available": True, "max": 4},
                            "book": {"available": False, "max": 0},
                            "collectiveSell": {"available": True, "min": 2, "max": 5},
                        },
                        {
                            "id": 3,
                            "sell": {"available": False, "max": 9},
                            "book": {"available": True, "max": 9},
                            "collectiveSell": {"available": False},
                        },
                    ]
                }
            ]
        }
    }
    capabilities = parse_sale_capabilities(payload, session_id="2")

    assert capabilities.allows_regular_sale(4)
    assert not capabilities.allows_regular_sale(5)
    assert capabilities.book_available is False
    assert capabilities.collective_sell_max == 5
    with pytest.raises(QuickTicketsContractError, match="absent"):
        parse_sale_capabilities(payload, session_id="missing")


def test_sale_capabilities_accept_requested_session_response_level_fields() -> None:
    payload = {
        "response": {
            "events": [{"sessions": [{"id": 3152}, {"id": 3153}]}],
            "sell": {"available": True, "max": 4},
            "book": {"available": False, "max": 0},
            "collectiveSell": {"available": True, "min": 2, "max": 30},
        }
    }
    capabilities = parse_sale_capabilities(payload, session_id="3152")

    assert capabilities.allows_regular_sale(1)
    assert capabilities.sell_max == 4
    assert capabilities.collective_sell_max == 30
    with pytest.raises(QuickTicketsContractError, match="absent"):
        parse_sale_capabilities(payload, session_id="missing")


def test_sale_capabilities_reject_mixed_or_partial_locations() -> None:
    response = {
        "events": [{"sessions": [{"id": 3152, "sell": {"available": True, "max": 4}}]}],
        "sell": {"available": True, "max": 4},
        "book": {"available": False, "max": 0},
        "collectiveSell": {"available": False},
    }
    with pytest.raises(QuickTicketsContractError, match="ambiguous"):
        parse_sale_capabilities({"response": response}, session_id="3152")


def test_long_lock_is_a_held_seat_not_a_free_or_unknown_seat() -> None:
    inventory = parse_inventory(
        _hall(),
        {"response": {"places": {"1": {"status": "long_lock"}}}},
        expected_hall_id="16",
    )
    assert inventory.seats[0].availability is SeatAvailability.HELD
