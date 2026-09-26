"""Normalize the three observed read-only QuickTickets inventory endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from theater_tickets.adapters.quicktickets.client import QuickTicketsClient, QuickTicketsContext
from theater_tickets.adapters.quicktickets.errors import QuickTicketsContractError
from theater_tickets.domain.models import Money, SaleCapabilities, Seat, SeatAvailability


@dataclass(frozen=True, slots=True)
class HallInventory:
    """A non-atomic but complete read of geometry and its availability overlay."""

    hall_id: str
    seats: tuple[Seat, ...]
    read_at: datetime


def _response(payload: dict[str, Any], endpoint: str) -> dict[str, Any]:
    response = payload.get("response")
    if not isinstance(response, dict):
        raise QuickTicketsContractError(f"{endpoint} response is missing")
    return response


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise QuickTicketsContractError(f"{field} must be an integer")
    if not isinstance(value, (int, str)):
        raise QuickTicketsContractError(f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise QuickTicketsContractError(f"{field} must be an integer") from exc


def _required_text(item: dict[str, Any], field: str) -> str:
    value = item.get(field)
    if value is None or not str(value).strip():
        raise QuickTicketsContractError(f"hall place {field} is missing")
    return str(value)


def _place_labels(item: dict[str, Any]) -> tuple[str, str]:
    """A hall place may be unnumbered, but a partially labelled place is invalid."""
    row, number = item.get("series"), item.get("place")
    if not isinstance(row, (str, int)) or not isinstance(number, (str, int)):
        raise QuickTicketsContractError("hall place row and number are missing")
    labels = str(row).strip(), str(number).strip()
    if bool(labels[0]) != bool(labels[1]):
        raise QuickTicketsContractError("hall place row and number are incomplete")
    return labels


def _price(value: object) -> Money:
    if isinstance(value, bool) or value is None:
        raise QuickTicketsContractError("hall place price is missing")
    try:
        return Money.from_rubles(Decimal(str(value)))
    except (InvalidOperation, ValueError) as exc:
        raise QuickTicketsContractError("hall place price is invalid") from exc


def _availability(base_status: object, overlay_status: object | None) -> SeatAvailability:
    if base_status == "disabled":
        return SeatAvailability.DISABLED
    status = overlay_status if overlay_status is not None else base_status
    if not isinstance(status, str):
        return SeatAvailability.UNKNOWN
    return {
        "free": SeatAvailability.FREE,
        "sell": SeatAvailability.SOLD,
        "short_lock": SeatAvailability.HELD,
        "long_lock": SeatAvailability.HELD,
        "lock": SeatAvailability.HELD,
        "disabled": SeatAvailability.DISABLED,
    }.get(status, SeatAvailability.UNKNOWN)


def parse_inventory(
    hall_payload: dict[str, Any],
    availability_payload: dict[str, Any],
    *,
    expected_hall_id: str | None = None,
) -> HallInventory:
    """Overlay a complete availability map onto hall geometry without guessing gaps.

    A current public ``hall/hall`` response may omit its hall ID. The caller
    can then pass the ID already verified in concrete session details; it is
    never inferred from seats or geometry.
    """
    hall = _response(hall_payload, "hall/hall")
    response_hall_id = hall.get("hall_id") or hall.get("id")
    if (
        response_hall_id is not None
        and expected_hall_id is not None
        and str(response_hall_id) != str(expected_hall_id)
    ):
        raise QuickTicketsContractError("hall/hall hall ID conflicts with session details")
    hall_id = response_hall_id if response_hall_id is not None else expected_hall_id
    if hall_id is None:
        raise QuickTicketsContractError("hall ID is absent from response and session details")
    raw_places = hall.get("places")
    if isinstance(raw_places, list):
        places = raw_places
    elif isinstance(raw_places, dict):
        places = []
        for mapping_id, item in raw_places.items():
            if not isinstance(item, dict) or str(item.get("id")) != str(mapping_id):
                raise QuickTicketsContractError("hall/hall places mapping has an invalid ID")
            places.append(item)
    else:
        raise QuickTicketsContractError("hall/hall response places must be a list or ID mapping")
    availability = _response(availability_payload, "anyticket/anyticket").get("places")
    if not isinstance(availability, dict):
        raise QuickTicketsContractError("anyticket/anyticket places must be a complete object")

    seats: list[Seat] = []
    seen: set[str] = set()
    for item in places:
        if not isinstance(item, dict):
            raise QuickTicketsContractError("hall/hall place must be an object")
        provider_id = _required_text(item, "id")
        if provider_id in seen:
            raise QuickTicketsContractError("hall/hall has duplicate place IDs")
        seen.add(provider_id)
        overlay = availability.get(provider_id)
        if overlay is None:
            overlay = availability.get(str(_integer(item["id"], "place id")))
        if overlay is not None and not isinstance(overlay, dict):
            raise QuickTicketsContractError("availability place must be an object")
        overlay_status = overlay.get("status") if overlay else None
        row_label, seat_label = _place_labels(item)
        seats.append(
            Seat(
                provider_id=provider_id,
                hall_id=str(hall_id),
                block=_required_text(item, "block"),
                row_label=row_label,
                seat_label=seat_label,
                price=_price(item.get("price")),
                availability=_availability(item.get("status"), overlay_status),
                x=_integer(item["x"], "x") if "x" in item else None,
                y=_integer(item["y"], "y") if "y" in item else None,
                width=_integer(item["width"], "width") if "width" in item else None,
                height=_integer(item["height"], "height") if "height" in item else None,
                rotation=_integer(item["rotate"], "rotate") if "rotate" in item else None,
            )
        )
    return HallInventory(str(hall_id), tuple(seats), datetime.now(UTC))


def _capability(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("available"), bool):
        raise QuickTicketsContractError(f"anysession/anysession {name} capability is invalid")
    return value


def parse_sale_capabilities(payload: dict[str, Any], *, session_id: str) -> SaleCapabilities:
    """Read the requested session's ordinary and collective sale constraints."""
    response = _response(payload, "anysession/anysession")
    events = response.get("events")
    if not isinstance(events, list):
        raise QuickTicketsContractError("anysession/anysession events must be a list")
    matching: dict[str, Any] | None = None
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("sessions"), list):
            continue
        for session in event["sessions"]:
            if isinstance(session, dict) and str(session.get("id")) == session_id:
                matching = session
                break
    if matching is None:
        raise QuickTicketsContractError("requested session is absent from sale capabilities")
    names = ("sell", "book", "collectiveSell")
    session_present = tuple(name in matching for name in names)
    response_present = tuple(name in response for name in names)
    if all(session_present) and not any(response_present):
        capabilities = matching
    elif not any(session_present) and all(response_present):
        capabilities = response
    else:
        raise QuickTicketsContractError("anysession/anysession sale capabilities are ambiguous")
    sell, book, collective = (
        _capability(capabilities.get("sell"), "sell"),
        _capability(capabilities.get("book"), "book"),
        _capability(capabilities.get("collectiveSell"), "collectiveSell"),
    )
    return SaleCapabilities(
        sell_available=sell["available"],
        sell_max=_integer(sell.get("max"), "sell.max"),
        book_available=book["available"],
        book_max=_integer(book.get("max"), "book.max"),
        collective_sell_available=collective["available"],
        collective_sell_min=_integer(collective["min"], "collectiveSell.min")
        if "min" in collective
        else None,
        collective_sell_max=_integer(collective["max"], "collectiveSell.max")
        if "max" in collective
        else None,
    )


class QuickTicketsInventoryReader:
    """Fetch the observed endpoint trio; one failed response yields no inventory."""

    def __init__(self, client: QuickTicketsClient) -> None:
        self._client = client

    async def fetch_inventory(
        self, *, context: QuickTicketsContext, session_id: str, hall_id: str | None = None
    ) -> HallInventory:
        hall, availability = (
            await self._client.get_json("hall/hall", context=context, elem_id=session_id),
            await self._client.get_json("anyticket/anyticket", context=context, elem_id=session_id),
        )
        return parse_inventory(hall, availability, expected_hall_id=hall_id)

    async def fetch_sale_capabilities(
        self, *, context: QuickTicketsContext, session_id: str
    ) -> SaleCapabilities:
        payload = await self._client.get_json(
            "anysession/anysession", context=context, elem_id=session_id
        )
        return parse_sale_capabilities(payload, session_id=session_id)
