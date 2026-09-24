"""Apply the requested centre-then-edge pair priority plan to a stalls profile."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

PRIMARY_ROWS = (
    ("8", 6),
    ("7", 6),
    ("9", 6),
    ("6", 6),
    ("10", 6),
    ("11", 6),
    ("12", 5),
    ("13", 4),
    ("14", 3),
    ("15", 2),
)
SECONDARY_ROWS = (("5", 6), ("4", 3), ("3", 4), ("2", 3), ("1", 2))


def _centre_pairs(seats: list[str], trim: int) -> list[tuple[str, str]]:
    starts = range(trim, len(seats) - trim - 1)
    centre = (len(seats) - 1) / 2
    ordered = sorted(starts, key=lambda index: (abs(index + 0.5 - centre), index))
    return [(seats[start], seats[start + 1]) for start in ordered]


def _edge_pairs(seats: list[str], trim: int) -> list[tuple[str, str]]:
    left = [(seats[index], seats[index + 1]) for index in range(max(trim - 1, 0))]
    right_start = len(seats) - trim
    right = [(seats[index], seats[index + 1]) for index in range(right_start, len(seats) - 1)]
    result: list[tuple[str, str]] = []
    for index in range(max(len(left), len(right))):
        if index < len(left):
            result.append(left[index])
        if index < len(right):
            result.append(right[-index - 1])
    return result


def _pairs(
    rows: dict[str, list[str]], plan: tuple[tuple[str, int], ...], edge: bool
) -> list[tuple[str, tuple[str, str]]]:
    result: list[tuple[str, tuple[str, str]]] = []
    for row, trim in plan:
        seats = rows[row]
        candidates = _edge_pairs(seats, trim) if edge else _centre_pairs(seats, trim)
        result.extend((row, pair) for pair in candidates)
    return result


def _balcony_pairs(rows: dict[str, list[str]]) -> list[tuple[str, tuple[str, str]]]:
    """Keep every balcony pair below stalls: centre pairs first, then edge pairs."""
    centre: list[tuple[str, tuple[str, str]]] = []
    edges: list[tuple[str, tuple[str, str]]] = []
    for row in sorted(rows, key=int):
        seats = rows[row]
        starts = list(range(len(seats) - 1))
        midpoint = (len(seats) - 1) / 2
        ordered = sorted(starts, key=lambda index: (abs(index + 0.5 - midpoint), index))
        centre.extend((row, (seats[index], seats[index + 1])) for index in ordered[:2])
        edge_order = sorted(ordered[2:], key=lambda index: (-abs(index + 0.5 - midpoint), index))
        edges.extend((row, (seats[index], seats[index + 1])) for index in edge_order)
    return centre + edges


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    args = parser.parse_args()
    raw = yaml.safe_load(args.profile.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("row_segments"), list):
        raise ValueError("profile has no row segments")
    rows = {
        str(segment["row"]): [str(seat) for seat in segment["seat_ids"]]
        for segment in raw["row_segments"]
        if isinstance(segment, dict) and segment.get("block") == "Партер"
    }
    balcony_rows = {
        str(segment["row"]): [str(seat) for seat in segment["seat_ids"]]
        for segment in raw["row_segments"]
        if isinstance(segment, dict) and segment.get("block") == "Балкон"
    }
    plan = PRIMARY_ROWS + SECONDARY_ROWS
    missing = [row for row, _ in plan if row not in rows]
    if missing:
        raise ValueError(f"stalls rows are missing: {', '.join(missing)}")
    ordered = (
        _pairs(rows, plan, edge=False)
        + _pairs(rows, PRIMARY_ROWS, edge=True)
        + _pairs(rows, SECONDARY_ROWS, edge=True)
        + _balcony_pairs(balcony_rows)
    )
    raw["mode"] = "manual_then_auto"
    raw["preferred_groups"] = [
        {"id": f"stalls-r{row}-pair-{index + 1}", "priority": index, "seat_ids": list(pair)}
        for index, (row, pair) in enumerate(ordered)
    ]
    args.profile.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    print(f"wrote {len(ordered)} prioritized pairs to {args.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
