import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Self
from weakref import finalize

from sqlalchemy import create_engine, event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from oms_hub.models import Base


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
