"""Exercise the exact stdin entry point used before uploading a new image."""

import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "backup-database.py"


def run_backup(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-", str(source), str(destination)],
        input=SCRIPT.read_text(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_deployment_backup_preserves_committed_wal(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backups" / "snapshot.sqlite3"
    with sqlite3.connect(source) as active:
        active.execute("PRAGMA journal_mode=WAL")
        active.execute("PRAGMA wal_autocheckpoint=0")
        active.execute("CREATE TABLE orders (id TEXT PRIMARY KEY)")
        active.execute("INSERT INTO orders VALUES ('committed')")
        active.commit()
        active.execute("INSERT INTO orders VALUES ('uncommitted')")
        result = run_backup(source, destination)
        assert result.returncode == 0, result.stderr
        active.rollback()
    assert destination.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(destination) as restored:
        assert restored.execute("SELECT id FROM orders").fetchall() == [("committed",)]
        assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    original = destination.read_bytes()
    assert run_backup(source, destination).returncode != 0
    assert destination.read_bytes() == original
    assert list(destination.parent.iterdir()) == [destination]


def test_deployment_backup_does_not_publish_failed_copy(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backups" / "snapshot.sqlite3"
    assert run_backup(source, destination).returncode != 0
    assert not source.exists()
    source.write_text("invalid database")
    result = run_backup(source, destination)
    assert result.returncode != 0
    assert "Database backup failed: DatabaseError" in result.stderr
    assert list(destination.parent.iterdir()) == []
