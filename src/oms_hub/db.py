from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self
from weakref import finalize

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from oms_hub.models import Base


class Database:
    def __init__(self, url: str):
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_engine(url, connect_args=connect_args)
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
