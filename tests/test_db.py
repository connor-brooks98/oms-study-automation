import sqlite3

from sqlalchemy.exc import OperationalError

from oms_hub.db import Database, is_sqlite_busy


def test_sqlite_connections_enable_safety_pragmas(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    try:
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
    finally:
        database.close()


def test_sqlite_busy_classification_uses_driver_error_code():
    busy = sqlite3.OperationalError("translated text is irrelevant")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    wrapped = OperationalError("statement", {}, busy)

    locked = sqlite3.OperationalError("also irrelevant")
    locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED

    assert is_sqlite_busy(wrapped)
    assert is_sqlite_busy(locked)
    assert not is_sqlite_busy(RuntimeError("database is locked"))
