import importlib.util
import json
import sqlite3
import sys
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


def test_installer_backup_logical_digest_ignores_physical_sqlite_churn(tmp_path):
    helper = _load_installer_backup_helper()
    source = tmp_path / "source.db"
    before = tmp_path / "before.db"
    after = tmp_path / "after.db"

    with closing(sqlite3.connect(source)) as connection:
        connection.execute(
            "CREATE TABLE sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO sentinel VALUES (1, 'stable')")
        connection.commit()

    before_physical = helper.backup_database(source, before)
    helper.backup_database(source, after)
    # Simulate physical-only SQLite header churn while preserving a valid
    # database. The change counter and version-valid-for fields form a pair.
    with after.open("r+b") as stream:
        stream.seek(24)
        change_counter = int.from_bytes(stream.read(4), "big")
        updated_counter = ((change_counter + 1) % (2**32)).to_bytes(4, "big")
        stream.seek(24)
        stream.write(updated_counter)
        stream.seek(92)
        stream.write(updated_counter)
    after_physical = helper._sha256(after)

    assert before_physical != after_physical
    assert helper.logical_sha256(before) == helper.logical_sha256(after)


def test_installer_backup_logical_digest_detects_data_and_schema_changes(tmp_path):
    helper = _load_installer_backup_helper()
    database = tmp_path / "hub.db"

    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE sentinel (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO sentinel VALUES (1, 'before')")
        connection.commit()

    initial = helper.logical_sha256(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE sentinel SET value = 'after' WHERE id = 1")
        connection.commit()
    data_changed = helper.logical_sha256(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE INDEX sentinel_value ON sentinel(value)")
        connection.commit()
    schema_changed = helper.logical_sha256(database)

    assert data_changed != initial
    assert schema_changed != data_changed


def test_installer_backup_logical_digest_includes_accessible_implicit_rowid(tmp_path):
    helper = _load_installer_backup_helper()
    database = tmp_path / "hub.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(rowid, value) VALUES (1, 'stable')")
        connection.commit()

    initial = helper.logical_sha256(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE sentinel SET rowid = 2 WHERE rowid = 1")
        connection.commit()

    assert helper.logical_sha256(database) != initial


def test_installer_backup_logical_digest_includes_autoincrement_sequence(tmp_path):
    helper = _load_installer_backup_helper()
    database = tmp_path / "hub.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE sentinel (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)"
        )
        connection.execute("INSERT INTO sentinel(value) VALUES ('stable')")
        connection.commit()

    initial = helper.logical_sha256(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE sqlite_sequence SET seq = 100 WHERE name = 'sentinel'")
        connection.commit()

    assert helper.logical_sha256(database) != initial


def test_installer_backup_logical_digest_includes_persistent_header_metadata(tmp_path):
    helper = _load_installer_backup_helper()
    database = tmp_path / "hub.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES ('stable')")
        connection.execute("PRAGMA user_version = 17")
        connection.execute("PRAGMA application_id = 1179862066")
        connection.commit()

    initial = helper.logical_sha256(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA user_version = 18")
        connection.commit()
    user_version_changed = helper.logical_sha256(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA application_id = 1179862067")
        connection.commit()

    assert user_version_changed != initial
    assert helper.logical_sha256(database) != user_version_changed


def test_installer_backup_cli_publishes_bound_physical_and_logical_hashes(
    tmp_path, monkeypatch, capsys
):
    helper = _load_installer_backup_helper()
    source = tmp_path / "source.db"
    destination = tmp_path / "backup" / "hub.db"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE sentinel (value BLOB NOT NULL)")
        connection.execute("INSERT INTO sentinel VALUES (?)", (b"stable",))
        connection.commit()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backup-sqlite.py",
            "--source",
            str(source),
            "--destination",
            str(destination),
        ],
    )

    assert helper.main() == 0
    proof = json.loads(capsys.readouterr().out)
    assert proof == {
        "destination": str(destination.resolve()),
        "logical_sha256": helper.logical_sha256(destination),
        "sha256": helper._sha256(destination),
        "status": "ok",
    }
