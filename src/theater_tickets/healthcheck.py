"""Fast SQLite healthcheck with no application or migration imports."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

SQLITE_URL_PREFIX = "sqlite+aiosqlite:///"
EXPECTED_SCHEMA_REVISION = "0015_individual_ticket_orders"


def check_database(database_url: str) -> str:
    if not database_url.startswith(SQLITE_URL_PREFIX):
        raise ValueError("DATABASE_URL must be a file-backed sqlite+aiosqlite URL")
    raw_path = database_url.removeprefix(SQLITE_URL_PREFIX)
    if not raw_path or raw_path == ":memory:":
        raise ValueError("DATABASE_URL must be a file-backed sqlite+aiosqlite URL")
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError("database file does not exist")
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2.0) as connection:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    revision = row[0] if row and isinstance(row[0], str) else None
    if revision != EXPECTED_SCHEMA_REVISION:
        raise RuntimeError("database schema is not at the expected revision")
    return revision


def main() -> int:
    try:
        revision = check_database(os.environ.get("DATABASE_URL", ""))
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 2
    print(f"health: ok; revision={revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
