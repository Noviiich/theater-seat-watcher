"""Migrations, smoke checks and recoverable SQLite backup/restore operations."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url


@dataclass(frozen=True, slots=True)
class DatabaseSmokeResult:
    path: Path
    revision: str


def sqlite_path(database_url: str) -> Path:
    """Resolve only the file-backed async SQLite URL supported by this deployment."""
    url = make_url(database_url)
    if url.drivername != "sqlite+aiosqlite" or not url.database or url.database == ":memory:":
        raise ValueError("DATABASE_URL must be a file-backed sqlite+aiosqlite URL")
    return Path(url.database).expanduser().resolve()


def migrate_database(database_url: str, *, project_root: Path | None = None) -> None:
    path = sqlite_path(database_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(path, project_root=project_root), "head")
    _chmod_private(path)


def smoke_database(
    database_url: str,
    *,
    project_root: Path | None = None,
) -> DatabaseSmokeResult:
    path = sqlite_path(database_url)
    if not path.is_file():
        raise RuntimeError("database file does not exist; run migrations first")
    with sqlite3.connect(path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise RuntimeError("database integrity check failed")
        revision_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    revision = revision_row[0] if revision_row else None
    config = _alembic_config(path, project_root=project_root)
    head = ScriptDirectory.from_config(config).get_current_head()
    if revision != head or head is None:
        raise RuntimeError("database schema is not at the application migration head")
    return DatabaseSmokeResult(path, head)


def backup_database(database_url: str, destination: Path) -> Path:
    """Create a consistent owner-only copy using SQLite's online backup API."""
    source = sqlite_path(database_url)
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise RuntimeError("source database does not exist")
    if destination.exists():
        raise FileExistsError("backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        _sqlite_backup(source, temporary)
        _verify_integrity(temporary)
        _chmod_private(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def restore_database(database_url: str, backup: Path) -> Path | None:
    """Restore a verified backup and retain a timestamped pre-restore safety copy."""
    target = sqlite_path(database_url)
    backup = backup.expanduser().resolve()
    if not backup.is_file():
        raise FileNotFoundError("backup file does not exist")
    _verify_integrity(backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    safety_copy: Path | None = None
    if target.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safety_copy = target.with_name(f"{target.name}.pre-restore-{stamp}.bak")
        suffix = 1
        while safety_copy.exists():
            safety_copy = target.with_name(f"{target.name}.pre-restore-{stamp}-{suffix}.bak")
            suffix += 1
        backup_database(database_url, safety_copy)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.restore")
    try:
        _sqlite_backup(backup, temporary)
        _verify_integrity(temporary)
        _chmod_private(temporary)
        for sidecar_suffix in ("-wal", "-shm"):
            target.with_name(target.name + sidecar_suffix).unlink(missing_ok=True)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return safety_copy


def _sqlite_backup(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)


def _verify_integrity(path: Path) -> None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise RuntimeError("SQLite backup integrity check failed")


def _alembic_config(path: Path, *, project_root: Path | None) -> Config:
    root = (project_root or Path.cwd()).resolve()
    config_path = root / "alembic.ini"
    script_path = root / "migrations"
    if not config_path.is_file() or not script_path.is_dir():
        raise RuntimeError("alembic.ini and migrations directory are required")
    config = Config(str(config_path))
    config.set_main_option("script_location", str(script_path))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _chmod_private(path: Path) -> None:
    path.chmod(0o600)
