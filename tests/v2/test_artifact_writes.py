import hashlib
import os
import signal
import sys
from contextlib import contextmanager
from io import BytesIO
from multiprocessing import Event, Process, Queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from oms_hub.artifact_writes import (
    ArtifactWriteClaimLost,
    ArtifactWriteContended,
    ArtifactWriteCoordinator,
    _FileLock,
)
from oms_hub.artifacts import ArtifactService
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.models import (
    LectureArtifactWriteClaimModel,
    StudyRevisionModel,
    UploadBatchModel,
    UploadItemModel,
)
from oms_hub.repositories import CatalogRepository, LectureInput


class MemoryLock:
    def __init__(self, *, contended: bool = False) -> None:
        self.contended = contended
        self.released = False

    def acquire(self, path):  # type: ignore[no-untyped-def]
        if self.contended:
            raise ArtifactWriteContended("held")
        return BytesIO(b"0")

    def release(self, stream):  # type: ignore[no-untyped-def]
        self.released = True
        stream.close()


class FencedClaim:
    def __init__(self, fail_on: int | None = None) -> None:
        self.calls = 0
        self.fail_on = fail_on

    def assert_owned(self) -> None:
        self.calls += 1
        if self.fail_on == self.calls:
            raise ArtifactWriteClaimLost("replaced")


def _promoting_transcript(
    tmp_path: Path,
) -> tuple[ArtifactService, Database, Settings, int, int, Path, Path, Path]:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        study_root=tmp_path / "study",
    )
    database = Database(settings.database_url)
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Topic", "", None)
    )
    source = settings.data_dir / "artifacts" / "v2" / "transcripts" / "cleaned.txt"
    destination = settings.study_root / "Neuro" / "Exam 1" / "cleaned.txt"
    source.parent.mkdir(parents=True)
    destination.parent.mkdir(parents=True)
    source.write_text("cleaned transcript", encoding="utf-8")
    destination.write_text("partial canonical bytes", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with database.session() as session:
        session.add(UploadBatchModel(id="batch", kind="transcripts", state="complete"))
        session.add(
            UploadItemModel(
                id="item",
                batch_id="batch",
                kind="transcripts",
                original_filename="cleaned.txt",
                staged_path=str(source),
                sha256=digest,
                size_bytes=source.stat().st_size,
                state="complete",
                lecture_id=lecture_id,
                confidence=1,
                manual_assignment=True,
            )
        )
        session.flush()
        revision = StudyRevisionModel(
            upload_item_id="item",
            lecture_id=lecture_id,
            kind="transcripts",
            source_sha256=digest,
            immutable_source_path=str(source),
            derived_sha256=digest,
            immutable_derived_path=str(source),
            canonical_derived_path=str(destination),
            state="promoting",
            current=False,
        )
        session.add(revision)
        session.flush()
        revision_id = revision.id
    backup = destination.with_name(f".{destination.name}.oms-backup-{revision_id}")
    backup.write_text("prior canonical bytes", encoding="utf-8")
    return (
        ArtifactService(database, settings),
        database,
        settings,
        lecture_id,
        revision_id,
        source,
        destination,
        backup,
    )


def _hold_lock(
    database_url: str, data_dir: str, lecture_id: int, ready: Queue, stop: Event
) -> None:
    database = Database(database_url)
    settings = Settings(_env_file=None, data_dir=data_dir, database_url=database_url)
    with ArtifactWriteCoordinator(database, settings).claim(lecture_id, "holder"):
        ready.put(True)
        stop.wait(30)


def test_claim_fences_replaced_durable_owner(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
    )
    database = Database(settings.database_url)
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Topic", "", None)
    )
    coordinator = ArtifactWriteCoordinator(database, settings, MemoryLock())
    successor_bytes = tmp_path / "successor.txt"

    with coordinator.claim(lecture_id, "test") as claim:
        claim.assert_owned()
        with database.session() as session:
            stored = session.get(LectureArtifactWriteClaimModel, lecture_id)
            assert stored is not None
            stored.owner = "successor"
        successor_bytes.write_text("successor", encoding="utf-8")
        with pytest.raises(ArtifactWriteClaimLost):
            claim.assert_owned()
        # Any cleanup path must fence first; a stale owner never reaches this
        # successor-created path.
        assert successor_bytes.read_text(encoding="utf-8") == "successor"


def test_contended_adapter_never_creates_durable_claim(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
    )
    database = Database(settings.database_url)
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Topic", "", None)
    )
    coordinator = ArtifactWriteCoordinator(database, settings, MemoryLock(contended=True))

    with pytest.raises(ArtifactWriteContended):
        with coordinator.claim(lecture_id, "test"):
            pass
    with database.session() as session:
        assert session.get(LectureArtifactWriteClaimModel, lecture_id) is None


def test_posix_process_contention_and_killed_holder_recovery(tmp_path):
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "data", database_url=f"sqlite:///{tmp_path / 'hub.db'}"
    )
    database = Database(settings.database_url)
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Neuro", 1, 1, "Topic", "", None)
    )
    ready: Queue = Queue()
    stop = Event()
    holder = Process(
        target=_hold_lock,
        args=(settings.database_url, str(settings.data_dir), lecture_id, ready, stop),
    )
    holder.start()
    assert ready.get(timeout=10) is True
    with pytest.raises(ArtifactWriteContended):
        with ArtifactWriteCoordinator(database, settings).claim(lecture_id, "contender"):
            pass
    os.kill(holder.pid, signal.SIGKILL)
    holder.join(timeout=10)
    with ArtifactWriteCoordinator(database, settings).claim(lecture_id, "successor") as claim:
        claim.assert_owned()


def test_windows_lock_adapter_initializes_and_unlocks_byte_zero(tmp_path, monkeypatch):
    calls: list[tuple[int, int, int]] = []
    fake = SimpleNamespace(
        LK_NBLCK=1,
        LK_UNLCK=2,
        locking=lambda fd, mode, size: calls.append((fd, mode, size)),
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    adapter = _FileLock(platform="nt")
    path = tmp_path / "lock"
    handle = adapter.acquire(path)
    adapter.release(handle)
    assert path.read_bytes() == b"\0"
    assert [call[1:] for call in calls] == [(1, 1), (2, 1)]


def test_artifact_promotion_claim_loss_before_copy_never_writes_current(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("owner", encoding="utf-8")
    committed = []
    with pytest.raises(ArtifactWriteClaimLost):
        ArtifactService._promote_with_rollback(
            [(source, destination)], 1, lambda: committed.append(True), FencedClaim(1)
        )
    assert not destination.exists()
    assert committed == []


def test_artifact_promotion_stale_cleanup_preserves_successor_bytes(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("owner", encoding="utf-8")
    destination.write_text("prior", encoding="utf-8")
    claim = FencedClaim()

    def commit() -> None:
        destination.write_text("successor", encoding="utf-8")
        claim.fail_on = claim.calls + 1
        raise RuntimeError("commit failed")

    with pytest.raises(ArtifactWriteClaimLost):
        ArtifactService._promote_with_rollback([(source, destination)], 1, commit, claim)
    assert destination.read_text(encoding="utf-8") == "successor"


def test_recovery_is_contended_before_it_can_restore_or_reset_canonical_bytes(tmp_path):
    service, database, settings, lecture_id, revision_id, _, destination, backup = (
        _promoting_transcript(tmp_path)
    )
    with ArtifactWriteCoordinator(database, settings).claim(lecture_id, "holder"):
        with pytest.raises(ArtifactWriteContended):
            service.approve(revision_id)
    assert destination.read_text(encoding="utf-8") == "partial canonical bytes"
    assert backup.read_text(encoding="utf-8") == "prior canonical bytes"
    revision = service.repository.get_study_revision(revision_id)
    assert revision.state == "promoting"
    assert revision.current is False


def test_recovery_claim_loss_preserves_successor_canonical_bytes(tmp_path, monkeypatch):
    service, _, _, _, revision_id, _, destination, backup = _promoting_transcript(tmp_path)

    class SuccessorClaim:
        def __init__(self) -> None:
            self.calls = 0

        def assert_owned(self) -> None:
            self.calls += 1
            if self.calls == 4:
                destination.write_text("successor canonical bytes", encoding="utf-8")
                raise ArtifactWriteClaimLost("successor replaced recovery owner")

    claim = SuccessorClaim()

    @contextmanager
    def replaced_claim(*_args, **_kwargs):
        yield claim

    monkeypatch.setattr(ArtifactWriteCoordinator, "claim", replaced_claim)
    with pytest.raises(ArtifactWriteClaimLost, match="successor replaced recovery owner"):
        service.approve(revision_id)
    assert destination.read_text(encoding="utf-8") == "successor canonical bytes"
    assert backup.read_text(encoding="utf-8") == "prior canonical bytes"
    revision = service.repository.get_study_revision(revision_id)
    assert revision.state == "promoting"
    assert revision.current is False


def test_recovery_under_claim_commits_matching_canonical_bytes_and_cleans_backup(tmp_path):
    service, _, _, _, revision_id, source, destination, backup = _promoting_transcript(tmp_path)
    destination.write_bytes(source.read_bytes())

    recovered = service.approve(revision_id)

    assert recovered.current is True
    assert recovered.state == "current"
    assert destination.read_bytes() == source.read_bytes()
    assert not backup.exists()
