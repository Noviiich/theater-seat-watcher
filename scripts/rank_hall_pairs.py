"""Write every currently available adjacent pair in one hall profile's ranking order."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import yaml

from theater_tickets.adapters.quicktickets.catalogue import parse_catalogue
from theater_tickets.adapters.quicktickets.client import QuickTicketsClient
from theater_tickets.adapters.quicktickets.errors import QuickTicketsError
from theater_tickets.adapters.quicktickets.inventory import QuickTicketsInventoryReader
from theater_tickets.adapters.quicktickets.profiles import load_seat_selection_configuration
from theater_tickets.adapters.quicktickets.provider import parse_session_identity
from theater_tickets.domain.models import SeatAvailability
from theater_tickets.domain.seating.candidates import rank_groups


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path, help="YAML profile from config/halls")
    parser.add_argument("--theatre", default="orel-teatr-svobodnoe-prostranstvo")
    parser.add_argument("--output", type=Path, help="Output YAML path")
    parser.add_argument(
        "--available-only",
        action="store_true",
        help="Exclude occupied pairs instead of ranking every physical pair",
    )
    return parser


async def _rank(profile_path: Path, theatre: str, output: Path, available_only: bool) -> None:
    configuration = load_seat_selection_configuration(profile_path)
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0), trust_env=False)
    client = QuickTicketsClient(theatre_alias=theatre, http_client=http, min_request_interval=0.25)
    try:
        catalogue = parse_catalogue(await client.get_catalogue_page())
        reader = QuickTicketsInventoryReader(client)
        for reference in catalogue.sessions:
            try:
                context = await client.get_context(reference.session_id)
                details = await client.get_json(
                    "anysession/anysession", context=context, elem_id=reference.session_id
                )
                identity = parse_session_identity(details, session_id=reference.session_id)
                inventory = await reader.fetch_inventory(
                    context=context, session_id=reference.session_id, hall_id=identity.hall_id
                )
            except (QuickTicketsError, ValueError):
                continue
            if not configuration.profile.matches_inventory(inventory.seats):
                continue
            preferences = replace(configuration.preferences, ticket_count=2)
            ranking_seats = (
                inventory.seats
                if available_only
                else tuple(
                    replace(seat, availability=SeatAvailability.FREE) for seat in inventory.seats
                )
            )
            ranked = rank_groups(ranking_seats, configuration.profile, preferences)
            current = {
                seat.provider_id: seat.availability is SeatAvailability.FREE
                for seat in inventory.seats
            }
            payload = {
                "profile_id": configuration.profile.profile_id,
                "source_session_id": reference.session_id,
                "pair_count": len(ranked.groups),
                "pairs": [
                    {
                        "priority": index + 1,
                        "seat_ids": [seat.provider_id for seat in group.group.seats],
                        "currently_free": all(
                            current[seat.provider_id] for seat in group.group.seats
                        ),
                        "row": group.group.seats[0].row_label,
                        "block": group.group.seats[0].block,
                        "seat_labels": [seat.seat_label for seat in group.group.seats],
                        "center_x": str(
                            (
                                Decimal(group.group.seats[0].x or 0)
                                + Decimal(group.group.seats[-1].x or 0)
                            )
                            / Decimal(2)
                        ),
                        "total_rub": str(Decimal(group.group.total.minor_units) / Decimal(100)),
                        "quality": str(group.group.quality),
                        "reason": group.explanation,
                    }
                    for index, group in enumerate(ranked.groups)
                ],
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            print(f"created {output} from session {reference.session_id}")
            return
    finally:
        await client.aclose()
        await http.aclose()
    raise RuntimeError("no current session matches the requested hall profile")


def main() -> int:
    args = _parser().parse_args()
    output = args.output or args.profile.with_suffix(".pairs.yaml")
    asyncio.run(_rank(args.profile, args.theatre, output, args.available_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
