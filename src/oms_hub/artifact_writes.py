"""Fenced, cross-process exclusion for lecture canonical artifact writes."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.models import LectureArtifactWriteClaimModel
from oms_hub.routing import expanded_path


class ArtifactWriteContended(RuntimeError):
    """Another process holds the per-lecture canonical-write lock."""


class ArtifactWriteClaimLost(RuntimeError):
    """The durable fence no longer belongs to this lock holder."""


class _FileLock:
    def __init__(self, platform: str | None = None) -> None:
        self.platform = platform or os.name

    def acquire(self, path: Path) -> BinaryIO:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            if self.platform == "nt":
                import msvcrt

                # locking() locks the current one-byte region; make it exist
                # and seek to byte zero for both lock and unlock.
                handle.seek(0)
                if not handle.read(1):
                    handle.seek(0)
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError as error:
            handle.close()
            raise ArtifactWriteContended("canonical lecture artifact write is contended") from error

    def release(self, stream: BinaryIO) -> None:
        try:
            if self.platform == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class ArtifactWriteClaim:
    def __init__(
        self,
        database: Database,
        lecture_id: int,
        owner: str,
        handle: BinaryIO,
        adapter: _FileLock,
    ):
        self.database = database
        self.lecture_id = lecture_id
        self.owner = owner
        self._handle = handle
        self._adapter = adapter
        self._released = False

    def assert_owned(self) -> None:
        if self._released or getattr(self._handle, "closed", True):
            raise ArtifactWriteClaimLost("canonical artifact write lock is no longer held")
        with self.database.session() as session:
            claim = session.get(LectureArtifactWriteClaimModel, self.lecture_id)
            if claim is None or claim.owner != self.owner:
                raise ArtifactWriteClaimLost("canonical artifact write fence was replaced")

    def release(self) -> None:
        if self._released:
            return
        try:
            with self.database.session() as session:
                claim = session.get(LectureArtifactWriteClaimModel, self.lecture_id)
                if claim is not None and claim.owner == self.owner:
                    session.delete(claim)
        finally:
            self._released = True
            self._adapter.release(self._handle)


class ArtifactWriteCoordinator:
    def __init__(self, database: Database, settings: Settings, adapter: _FileLock | None = None):
        self.database = database
        self.settings = settings
        self.adapter = adapter or _FileLock()

    @contextmanager
    def claim(self, lecture_id: int, purpose: str) -> Iterator[ArtifactWriteClaim]:
        lock_path = (
            expanded_path(self.settings.data_dir) / "locks" / f"lecture-{lecture_id}.artifact.lock"
        )
        handle = self.adapter.acquire(lock_path)
        owner = str(uuid4())
        try:
            # Holding the OS lock proves a predecessor can no longer hold the
            # file lock.  Preserve the durable trail by updating it in place.
            with self.database.session() as session:
                previous = session.get(LectureArtifactWriteClaimModel, lecture_id)
                if previous is None:
                    session.add(
                        LectureArtifactWriteClaimModel(
                            lecture_id=lecture_id, owner=owner, purpose=purpose
                        )
                    )
                else:
                    previous.owner = owner
                    previous.purpose = f"takeover:{purpose}"
            claim = ArtifactWriteClaim(self.database, lecture_id, owner, handle, self.adapter)
            yield claim
        except BaseException:
            claim.release() if "claim" in locals() else self.adapter.release(handle)
            raise
        else:
            claim.release()
