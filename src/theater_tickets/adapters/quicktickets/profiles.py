"""Strict YAML loader for human-verified hall topology profiles."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from theater_tickets.application.booking import SeatSelectionConfiguration
from theater_tickets.domain.errors import DomainValidationError
from theater_tickets.domain.models import Seat
from theater_tickets.domain.seating.candidates import ScoringWeights, SelectionPreferences
from theater_tickets.domain.seating.topology import (
    HallProfile,
    PreferredGroup,
    RowSegment,
    SeatingMode,
)


def load_hall_profile(path: Path) -> HallProfile:
    """Load one profile; no defaults hide omissions in a live topology declaration."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DomainValidationError(f"cannot load hall profile: {path.name}") from exc
    if not isinstance(raw, dict):
        raise DomainValidationError("hall profile must be a YAML object")
    return parse_hall_profile(raw)


def load_seat_selection_configuration(path: Path) -> SeatSelectionConfiguration:
    """Load topology plus explicit scoring values used by production dry-run."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DomainValidationError(f"cannot load hall profile: {path.name}") from exc
    if not isinstance(raw, dict):
        raise DomainValidationError("hall profile must be a YAML object")
    profile = parse_hall_profile(raw)
    selection = raw.get("selection")
    if not isinstance(selection, dict):
        raise DomainValidationError("hall profile selection must be an object")
    weights = selection.get("weights")
    row_quality = selection.get("row_quality")
    aisle_quality = selection.get("aisle_quality")
    if not isinstance(weights, dict):
        raise DomainValidationError("hall profile selection weights must be an object")
    if not isinstance(row_quality, dict) or not isinstance(aisle_quality, dict):
        raise DomainValidationError("hall profile quality mappings must be objects")
    try:
        preferences = SelectionPreferences(
            ticket_count=1,
            max_ticket_price=None,
            max_order_total=None,
            row_quality={str(key): _decimal(value) for key, value in row_quality.items()},
            weights=ScoringWeights(
                _decimal(weights["row"]),
                _decimal(weights["center"]),
                _decimal(weights["price"]),
                _decimal(weights["aisle"]),
            ),
            min_quality=_decimal(selection["min_quality"]),
            view_axis_x=_decimal(selection["view_axis_x"]),
            normalization_width=_decimal(selection["normalization_width"]),
            aisle_quality={str(key): _decimal(value) for key, value in aisle_quality.items()},
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainValidationError("hall profile selection is incomplete or invalid") from exc
    return SeatSelectionConfiguration(profile, preferences)


class DirectorySeatProfileSource:
    """Resolve safe profile IDs to versioned YAML files in one configured directory."""

    _PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def load(self, profile_id: str) -> SeatSelectionConfiguration:
        if not self._PROFILE_ID.fullmatch(profile_id) or ".." in profile_id:
            raise LookupError("seat profile ID is invalid")
        path = self._directory / f"{profile_id}.yaml"
        try:
            return load_seat_selection_configuration(path)
        except DomainValidationError as exc:
            raise LookupError("seat profile is missing or invalid") from exc

    def find_matching(self, inventory: tuple[Seat, ...]) -> SeatSelectionConfiguration:
        """Return the first verified YAML profile whose fingerprint matches this hall."""
        for path in sorted(self._directory.glob("*.yaml")):
            try:
                configuration = load_seat_selection_configuration(path)
            except DomainValidationError:
                continue
            if configuration.profile.matches_inventory(inventory):
                return configuration
        raise LookupError("no configured seat profile matches the current hall")


def parse_hall_profile(raw: dict[str, Any]) -> HallProfile:
    """Convert a schema-checked mapping to pure topology objects."""
    allowed = {
        "profile_id",
        "hall_id",
        "topology_fingerprint",
        "mode",
        "row_segments",
        "preferred_groups",
        "selection",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise DomainValidationError(f"unknown hall profile fields: {', '.join(sorted(unknown))}")
    try:
        segments = tuple(_segment(item) for item in _list(raw, "row_segments"))
        groups = tuple(_group(item) for item in raw.get("preferred_groups", []))
        return HallProfile(
            profile_id=_text(raw, "profile_id"),
            hall_id=_text(raw, "hall_id"),
            topology_fingerprint=_text(raw, "topology_fingerprint"),
            mode=SeatingMode(_text(raw, "mode")),
            row_segments=segments,
            preferred_groups=groups,
        )
    except DomainValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainValidationError("hall profile has invalid required fields") from exc


def _text(raw: dict[str, Any], name: str) -> str:
    value = raw[name]
    if not isinstance(value, str):
        raise TypeError(name)
    return value


def _list(raw: dict[str, Any], name: str) -> list[dict[str, Any]]:
    value = raw[name]
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise TypeError(name)
    return value


def _seat_ids(raw: dict[str, Any]) -> tuple[str, ...]:
    value = raw.get("seat_ids")
    if not isinstance(value, list) or any(not isinstance(item, (str, int)) for item in value):
        raise TypeError("seat_ids")
    return tuple(str(item) for item in value)


def _segment(raw: dict[str, Any]) -> RowSegment:
    return RowSegment(_text(raw, "id"), _text(raw, "block"), _text(raw, "row"), _seat_ids(raw))


def _group(raw: dict[str, Any]) -> PreferredGroup:
    priority = raw.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise TypeError("priority")
    return PreferredGroup(_text(raw, "id"), priority, _seat_ids(raw))


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool | float) or not isinstance(value, str | int | Decimal):
        raise TypeError("decimal value")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal value") from exc
