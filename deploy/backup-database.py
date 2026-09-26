"""Online deployment backup, streamed into the existing container via stdin."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def backup(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".backup-", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        # Read-only mode must still read WAL; immutable=1 would omit recent writes.
        with (
            closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as reader,
            closing(sqlite3.connect(temporary)) as writer,
        ):
            reader.backup(writer, pages=256)
            if writer.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("SQLite backup integrity check failed")
            writer.execute("PRAGMA journal_mode=DELETE")
        with temporary.open("rb") as completed:
            os.fsync(completed.fileno())
        # Publish atomically without ever replacing an existing backup.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        backup(Path(sys.argv[1]), Path(sys.argv[2]))
    except Exception as error:
        print(f"Database backup failed: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
    print("Database backup verified")
