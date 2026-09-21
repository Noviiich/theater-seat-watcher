"""Safe configuration diagnostic; production runtime wiring is a deployment concern."""

from __future__ import annotations

from theater_tickets.bootstrap import create_application


def main() -> int:
    """Print the safe application status and exit."""
    print("\n".join(create_application().status_lines()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
