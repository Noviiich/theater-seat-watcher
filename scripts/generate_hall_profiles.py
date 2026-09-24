"""Generate conservative draft hall profiles from public QuickTickets reads only."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx
import yaml

from theater_tickets.adapters.quicktickets.catalogue import parse_catalogue
from theater_tickets.adapters.quicktickets.client import QuickTicketsClient
from theater_tickets.adapters.quicktickets.errors import QuickTicketsError
from theater_tickets.adapters.quicktickets.inventory import QuickTicketsInventoryReader
from theater_tickets.adapters.quicktickets.provider import parse_session_identity
from theater_tickets.domain.seating.topology import HallProfile, infer_conservative_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--theatre", default="orel-teatr-svobodnoe-prostranstvo")
    parser.add_argument("--output", type=Path, default=Path("config/halls"))
    return parser


def _payload(profile_id: str, inferred: HallProfile) -> dict[str, object]:
    return {
        "profile_id": profile_id,
        "hall_id": inferred.hall_id,
        "topology_fingerprint": inferred.topology_fingerprint,
        "mode": "automatic",
        "row_segments": [
            {
                "id": segment.segment_id,
                "block": segment.block,
                "row": segment.row_label,
                "seat_ids": list(segment.seat_ids),
            }
            for segment in inferred.row_segments
        ],
        "preferred_groups": [],
        "selection": {
            "row_quality": {segment.row_label: "1.00" for segment in inferred.row_segments},
            "weights": {"row": "0.20", "center": "0.40", "price": "0.40", "aisle": "0.00"},
            "min_quality": "0.00",
            "view_axis_x": "0",
            "normalization_width": "1",
            "aisle_quality": {segment.segment_id: "1.00" for segment in inferred.row_segments},
        },
    }


async def _generate(theatre: str, output: Path) -> int:
    output.mkdir(parents=True, exist_ok=True)
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), trust_env=False)
    client = QuickTicketsClient(
        theatre_alias=theatre,
        http_client=http_client,
        min_request_interval=0.25,
    )
    try:
        catalogue = parse_catalogue(await client.get_catalogue_page())
        if not catalogue.complete:
            raise RuntimeError("public catalogue is incomplete")
        inventory_reader = QuickTicketsInventoryReader(client)
        unique: dict[str, HallProfile] = {}
        for session in catalogue.sessions:
            try:
                context = await client.get_context(session.session_id)
                details = await client.get_json(
                    "anysession/anysession", context=context, elem_id=session.session_id
                )
                identity = parse_session_identity(details, session_id=session.session_id)
                inventory = await inventory_reader.fetch_inventory(
                    context=context,
                    session_id=session.session_id,
                    hall_id=identity.hall_id,
                )
                profile = infer_conservative_profile(inventory.seats)
            except (QuickTicketsError, ValueError) as exc:
                print(f"skipped session {session.session_id}: {type(exc).__name__}")
                continue
            unique.setdefault(profile.topology_fingerprint, profile)
        for fingerprint, profile in unique.items():
            profile_id = f"hall-{profile.hall_id}-{fingerprint[:12]}"
            path = output / f"{profile_id}.yaml"
            if path.exists():
                print(f"exists {path}")
                continue
            path.write_text(
                "# Draft generated from public QuickTickets geometry; review before live use.\n"
                + yaml.safe_dump(
                    _payload(profile_id, profile), allow_unicode=True, sort_keys=False
                ),
                encoding="utf-8",
            )
            print(f"created {path}")
    finally:
        await client.aclose()
        await http_client.aclose()
    return 0


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(_generate(args.theatre, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
