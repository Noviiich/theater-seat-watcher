"""Strict YAML loader for human-verified hall topology profiles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from theater_tickets.domain.errors import DomainValidationError
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


def parse_hall_profile(raw: dict[str, Any]) -> HallProfile:
    """Convert a schema-checked mapping to pure topology objects."""
    allowed = {
        "profile_id",
        "hall_id",
        "topology_fingerprint",
        "mode",
        "row_segments",
        "preferred_groups",
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
