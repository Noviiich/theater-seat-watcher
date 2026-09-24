"""Topology profiles are explicit: coordinates never silently imply adjacency."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from theater_tickets.domain.errors import DomainValidationError
from theater_tickets.domain.models import Seat


class SeatingMode(StrEnum):
    MANUAL_ONLY = "manual_only"
    AUTOMATIC = "automatic"
    MANUAL_THEN_AUTO = "manual_then_auto"


@dataclass(frozen=True, slots=True)
class RowSegment:
    """A visually verified contiguous sequence; a passage requires another segment."""

    segment_id: str
    block: str
    row_label: str
    seat_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.segment_id.strip() or not self.block.strip() or not self.row_label.strip():
            raise DomainValidationError("segment id, block, and row must not be blank")
        if not self.seat_ids or any(not value.strip() for value in self.seat_ids):
            raise DomainValidationError("row segment must contain non-blank seat IDs")
        if len(set(self.seat_ids)) != len(self.seat_ids):
            raise DomainValidationError("row segment must not repeat a seat ID")


@dataclass(frozen=True, slots=True)
class PreferredGroup:
    """An explicitly preferred contiguous group, ranked before automatic groups."""

    group_id: str
    priority: int
    seat_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.group_id.strip() or self.priority < 0:
            raise DomainValidationError(
                "preferred group id must be non-blank and priority non-negative"
            )
        if not self.seat_ids or any(not value.strip() for value in self.seat_ids):
            raise DomainValidationError("preferred group must contain non-blank seat IDs")
        if len(set(self.seat_ids)) != len(self.seat_ids):
            raise DomainValidationError("preferred group must not repeat a seat ID")


@dataclass(frozen=True, slots=True)
class HallProfile:
    """A profile accepted only when it matches the current verified hall topology."""

    profile_id: str
    hall_id: str
    topology_fingerprint: str
    mode: SeatingMode
    row_segments: tuple[RowSegment, ...]
    preferred_groups: tuple[PreferredGroup, ...] = ()

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.hall_id.strip():
            raise DomainValidationError("profile and hall IDs must not be blank")
        if len(self.topology_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in self.topology_fingerprint.lower()
        ):
            raise DomainValidationError("topology fingerprint must be a SHA-256 hex digest")
        if not self.row_segments:
            raise DomainValidationError("profile must have at least one row segment")
        if len({segment.segment_id for segment in self.row_segments}) != len(self.row_segments):
            raise DomainValidationError("profile must not repeat a segment ID")
        all_ids = tuple(seat_id for segment in self.row_segments for seat_id in segment.seat_ids)
        if len(set(all_ids)) != len(all_ids):
            raise DomainValidationError("seat ID must not appear in multiple row segments")
        if len({group.group_id for group in self.preferred_groups}) != len(self.preferred_groups):
            raise DomainValidationError("profile must not repeat a preferred group ID")
        position = {
            seat_id: (segment, index)
            for segment in self.row_segments
            for index, seat_id in enumerate(segment.seat_ids)
        }
        for group in self.preferred_groups:
            positions = [position.get(seat_id) for seat_id in group.seat_ids]
            if any(item is None for item in positions):
                raise DomainValidationError(
                    "preferred group references a seat outside row segments"
                )
            first = positions[0]
            if first is None:
                raise DomainValidationError(
                    "preferred group references a seat outside row segments"
                )
            segment = first[0]
            indices = [item[1] for item in positions if item is not None]
            if any(item is None or item[0] != segment for item in positions) or indices != list(
                range(indices[0], indices[0] + len(indices))
            ):
                raise DomainValidationError("preferred group must be contiguous in one row segment")

    def matches_inventory(self, seats: tuple[Seat, ...]) -> bool:
        """Reject profile use when the hall geometry or row labelling has changed."""
        if not seats or any(seat.hall_id != self.hall_id for seat in seats):
            return False
        if topology_fingerprint(seats) != self.topology_fingerprint:
            return False
        by_id = {seat.provider_id: seat for seat in seats}
        for segment in self.row_segments:
            for seat_id in segment.seat_ids:
                seat = by_id.get(seat_id)
                if (
                    seat is None
                    or seat.block != segment.block
                    or seat.row_label != segment.row_label
                ):
                    return False
        return True


def topology_fingerprint(seats: tuple[Seat, ...]) -> str:
    """Hash only physical identity/geometry, never changing availability or prices."""
    if not seats:
        raise DomainValidationError("cannot fingerprint an empty hall")
    hall_ids = {seat.hall_id for seat in seats}
    if len(hall_ids) != 1:
        raise DomainValidationError("cannot fingerprint seats from multiple halls")
    records = sorted(
        (
            seat.provider_id,
            seat.block,
            seat.row_label,
            seat.seat_label,
            seat.x,
            seat.y,
            seat.width,
            seat.height,
            seat.rotation,
        )
        for seat in seats
    )
    encoded = json.dumps(
        {"hall_id": next(iter(hall_ids)), "seats": records},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def infer_conservative_profile(seats: tuple[Seat, ...]) -> HallProfile:
    """Build segments only where geometry proves a short uninterrupted seat gap.

    This fallback intentionally splits uncertain rows rather than joining seats
    across a possible aisle. A checked YAML profile remains the richer option.
    """
    if not seats:
        raise DomainValidationError("cannot infer topology from an empty hall")
    hall_ids = {seat.hall_id for seat in seats}
    if len(hall_ids) != 1:
        raise DomainValidationError("cannot infer topology for multiple halls")
    rows: dict[tuple[str, str], list[Seat]] = {}
    for seat in seats:
        if seat.x is None or seat.width is None or not seat.block or not seat.row_label:
            continue
        rows.setdefault((seat.block, seat.row_label), []).append(seat)
    segments: list[RowSegment] = []
    for (block, row_label), row_seats in sorted(rows.items()):
        ordered = sorted(row_seats, key=lambda seat: (seat.x or 0, seat.provider_id))
        gaps = [
            (right.x or 0) - (left.x or 0)
            for left, right in zip(ordered, ordered[1:], strict=False)
            if (right.x or 0) > (left.x or 0)
        ]
        groups: list[list[Seat]]
        if not gaps:
            groups = [ordered]
        else:
            short_gap = min(gaps)
            groups = [[ordered[0]]]
            for previous, seat in zip(ordered, ordered[1:], strict=False):
                gap = (seat.x or 0) - (previous.x or 0)
                if gap > short_gap * 1.5:
                    groups.append([])
                groups[-1].append(seat)
        for index, group in enumerate(groups):
            if group:
                segments.append(
                    RowSegment(
                        f"inferred:{block}:{row_label}:{index}",
                        block,
                        row_label,
                        tuple(seat.provider_id for seat in group),
                    )
                )
    if not segments:
        raise DomainValidationError("hall geometry is insufficient for inferred topology")
    return HallProfile(
        profile_id="inferred",
        hall_id=next(iter(hall_ids)),
        topology_fingerprint=topology_fingerprint(seats),
        mode=SeatingMode.AUTOMATIC,
        row_segments=tuple(segments),
    )
