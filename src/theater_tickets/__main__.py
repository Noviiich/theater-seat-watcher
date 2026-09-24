"""Operator CLI for diagnostics, migrations, runtime and SQLite recovery."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from theater_tickets.bootstrap import create_application
from theater_tickets.operations.database import (
    backup_database,
    health_database,
    migrate_database,
    restore_database,
    smoke_database,
)
from theater_tickets.production import run_production
from theater_tickets.settings import Settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    command = arguments.command or "diagnose"
    try:
        settings = Settings.from_environ()
        if command == "diagnose":
            print("\n".join(create_application(settings).status_lines()))
            return 0
        database_url = _database_url(settings)
        if command == "migrate":
            migrate_database(database_url)
            print("database migrations: current")
            return 0
        if command == "smoke":
            result = smoke_database(database_url)
            print(f"dry-run smoke: ok; revision={result.revision}")
            return 0
        if command == "health":
            result = health_database(database_url)
            print(f"health: ok; revision={result.revision}")
            return 0
        if command == "backup":
            destination = backup_database(database_url, arguments.destination)
            print(f"database backup created: {destination.name}")
            return 0
        if command == "restore":
            if not arguments.yes:
                parser.error("restore requires --yes and the bot must be stopped")
            safety = restore_database(database_url, arguments.source)
            message = "database restored"
            if safety is not None:
                message += f"; previous database saved as {safety.name}"
            print(message)
            return 0
        if command == "run":
            settings.validate_runtime()
            migrate_database(database_url)
            smoke_database(database_url)
            logging.basicConfig(level=logging.INFO, format="%(message)s")
            asyncio.run(run_production(settings))
            return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    raise AssertionError("unhandled CLI command")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="theater-tickets")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("diagnose", help="print non-secret configuration status")
    subparsers.add_parser("migrate", help="upgrade the configured SQLite database")
    subparsers.add_parser("smoke", help="check SQLite integrity and migration head")
    subparsers.add_parser("health", help="check that live SQLite is readable and current")
    subparsers.add_parser("run", help="start the production dry-run runtime")
    backup = subparsers.add_parser("backup", help="create an online SQLite backup")
    backup.add_argument("destination", type=Path)
    restore = subparsers.add_parser("restore", help="restore SQLite while the bot is stopped")
    restore.add_argument("source", type=Path)
    restore.add_argument("--yes", action="store_true", help="confirm replacement of the database")
    return parser


def _database_url(settings: Settings) -> str:
    if settings.database_url is None:
        raise ValueError("DATABASE_URL is required for this command")
    return settings.database_url


if __name__ == "__main__":
    raise SystemExit(main())
