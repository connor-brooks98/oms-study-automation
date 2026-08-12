import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from oms_hub.db import backup_sqlite_database


def _load_installer_backup_helper():
    path = Path(__file__).parents[1] / "scripts" / "backup-sqlite.py"
    spec = importlib.util.spec_from_file_location("backup_sqlite", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_online_sqlite_backup_includes_committed_wal_pages_and_validates(tmp_path):
    source = tmp_path / "hub.db"
    destination = tmp_path / "backup" / "hub.db"
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("CREATE TABLE revisions (value TEXT NOT NULL)")
        writer.execute("INSERT INTO revisions VALUES ('wal-committed')")
        writer.commit()
        assert source.with_name("hub.db-wal").is_file()
        digest = backup_sqlite_database(source, destination)
    finally:
        writer.close()

    assert len(digest) == 64
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("SELECT value FROM revisions").fetchall() == [
            ("wal-committed",)
        ]
        assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        restored.close()


def test_installer_backup_helper_includes_wal_only_committed_page(tmp_path):
    helper = _load_installer_backup_helper()
    source = tmp_path / "installer.db"
    destination = tmp_path / "backup" / "installer.db"
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        writer.execute("INSERT INTO sentinel VALUES ('committed-in-wal')")
        writer.commit()
        assert source.with_name("installer.db-wal").is_file()
        digest = helper.backup_database(source, destination)
    finally:
        writer.close()

    assert len(digest) == 64
    assert not destination.with_name(f"{destination.name}-wal").exists()
    assert not destination.with_name(f"{destination.name}-shm").exists()
    with closing(
        sqlite3.connect(f"{destination.resolve().as_uri()}?mode=ro", uri=True)
    ) as restored:
        assert restored.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert restored.execute("SELECT value FROM sentinel").fetchall() == [
            ("committed-in-wal",)
        ]
        assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert not destination.with_name(f"{destination.name}-wal").exists()
    assert not destination.with_name(f"{destination.name}-shm").exists()


def test_installer_backup_helper_rejects_transient_destination_sidecar(tmp_path):
    helper = _load_installer_backup_helper()
    destination = tmp_path / "hub.db"
    sidecar = destination.with_name(f"{destination.name}-shm")
    sidecar.write_bytes(b"transient")

    with pytest.raises(sqlite3.DatabaseError, match="retained transient sidecars"):
        helper._assert_no_sidecars(destination)
