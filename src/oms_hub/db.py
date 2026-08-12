import hashlib
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from weakref import finalize

from sqlalchemy import create_engine, event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from oms_hub.models import Base


def backup_sqlite_database(source: Path, destination: Path) -> str:
    """Create an atomically promoted, integrity-checked online SQLite backup.

    ``Connection.backup`` copies committed WAL pages, unlike a filesystem copy
    of the main database file.  The returned digest is calculated only after
    the destination has passed ``integrity_check``.
    """
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.partial-{uuid.uuid4().hex}"
    )
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        source_connection = sqlite3.connect(source_uri, uri=True)
        try:
            destination_connection = sqlite3.connect(temporary)
            try:
                source_connection.backup(destination_connection)
                _assert_sqlite_integrity(destination_connection, temporary)
            finally:
                destination_connection.close()
        finally:
            source_connection.close()
        os.replace(temporary, destination)
        check_connection = sqlite3.connect(
            f"{destination.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        try:
            _assert_sqlite_integrity(check_connection, destination)
        finally:
            check_connection.close()
        return _sha256_path(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _assert_sqlite_integrity(connection: sqlite3.Connection, path: Path) -> None:
    result = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        raise sqlite3.DatabaseError(
            f"SQLite backup integrity check failed for {path}: {result}"
        )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Database:
    def __init__(self, url: str):
        sqlite_url = url.startswith("sqlite")
        connect_args = (
            {"check_same_thread": False, "timeout": 5.0}
            if sqlite_url
            else {}
        )
        engine_options: dict[str, Any] = {"connect_args": connect_args}
        if sqlite_url:
            engine_options["poolclass"] = (
                StaticPool if url in {"sqlite://", "sqlite:///:memory:"} else NullPool
            )
        self.engine = create_engine(url, **engine_options)
        if sqlite_url:
            event.listen(self.engine, "connect", _configure_sqlite_connection)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._finalizer = finalize(self, self.engine.dispose)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def migrate(self) -> None:
        from oms_hub.migrations import migrate_database

        migrate_database(self)

    def close(self) -> None:
        if self._finalizer.alive:
            self._finalizer()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def _configure_sqlite_connection(
    connection: sqlite3.Connection,
    _connection_record: object,
) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def is_sqlite_busy(error: BaseException) -> bool:
    current: BaseException | None = error
    if isinstance(current, DBAPIError) and isinstance(current.orig, BaseException):
        current = current.orig
    code = getattr(current, "sqlite_errorcode", None)
    if not isinstance(code, int):
        return False
    primary_code = code & 0xFF
    return primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
