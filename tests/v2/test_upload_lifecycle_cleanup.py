import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from oms_hub.db import Database
from oms_hub.ingestion.domain import (
    StagedUpload,
    UploadKind,
    UploadManifestSlot,
    UploadState,
)
from oms_hub.ingestion.matcher import UploadMatcher
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.service import IngestionService
from oms_hub.ingestion.staging import StagingService, UploadRejected
from oms_hub.models import StudyRevisionModel, UploadItemModel
from oms_hub.repositories import CatalogRepository, LectureInput


def _prepared(tmp_path: Path) -> tuple[IngestionRepository, IngestionService, int]:
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    catalog = CatalogRepository(database)
    lecture_id = catalog.upsert_lecture(
        LectureInput("Cardiology", 1, 7, "Heart Failure", "Dr Test", None)
    )
    repository = IngestionRepository(database)
    service = IngestionService(
        repository,
        catalog,
        UploadMatcher(),
        StagingService(tmp_path / "staging", 1_000_000, 2_000_000),
    )
    return repository, service, lecture_id


def _add_item(
    repository: IngestionRepository,
    root: Path,
    batch_id: str,
    item_id: str,
    lecture_id: int,
) -> Path:
    path = root / f"{item_id}.ready"
    payload = b"valid transcript"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(
            batch_id,
            item_id,
            path,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            f"{item_id}.txt",
        ),
    )
    repository.set_manual_assignment(item_id, lecture_id)
    return path


def test_failed_sibling_does_not_make_batch_lifecycle_terminal(tmp_path: Path) -> None:
    repository, _, lecture_id = _prepared(tmp_path)
    batch_id = repository.create_batch(UploadKind.TRANSCRIPTS)
    _add_item(repository, tmp_path / "staging", batch_id, "failed", lecture_id)
    _add_item(repository, tmp_path / "staging", batch_id, "queued", lecture_id)
    claimed = repository.claim_next_job(datetime.now(UTC))
    assert claimed is not None
    repository.fail_job(claimed, "induced", state=UploadState.FAILED)

    batch = repository.get_batch(batch_id)

    assert batch is not None
    assert batch.state is UploadState.FAILED  # legacy severity compatibility
    assert batch.lifecycle == "active"
    assert batch.outcome == "failed"


def test_expired_chunk_sessions_are_collected_idempotently(tmp_path: Path) -> None:
    _, service, _ = _prepared(tmp_path)
    session = service.staging.begin_chunks(
        UploadKind.TRANSCRIPTS,
        "expired.txt",
        4,
        hashlib.sha256(b"text").hexdigest(),
    )
    session_path = service.staging.root / "chunks" / f"{session.id}.json"
    chunk_path = service.staging.root / "chunks" / f"{session.id}.part"
    assert session_path.exists() and chunk_path.exists()

    removed = service.collect_staging(datetime.now(UTC) + timedelta(hours=25))

    assert removed == 2
    assert not session_path.exists()
    assert not chunk_path.exists()
    assert not (service.staging.root / "batches" / session.batch_id).exists()
    assert service.collect_staging(datetime.now(UTC) + timedelta(hours=25)) == 0


def test_expired_legacy_chunk_preserves_a_repository_referenced_batch(
    tmp_path: Path,
) -> None:
    repository, service, _ = _prepared(tmp_path)
    session = service.staging.begin_chunks(
        UploadKind.TRANSCRIPTS,
        "referenced.txt",
        4,
        hashlib.sha256(b"text").hexdigest(),
    )
    repository.create_batch(UploadKind.TRANSCRIPTS, session.batch_id)
    batch_root = service.staging.root / "batches" / session.batch_id

    assert service.collect_staging(datetime.now(UTC) + timedelta(hours=25)) == 1
    assert batch_root.exists()


def test_expired_pending_manifest_reconciles_precommit_orphan_batch(
    tmp_path: Path,
) -> None:
    _, service, _ = _prepared(tmp_path)
    payload = b"precommit manifest"
    manifest = service.staging.begin_manifest(
        UploadKind.TRANSCRIPTS,
        [
            UploadManifestSlot(
                "00000000-0000-0000-0000-000000000004",
                "precommit.txt",
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )
        ],
    )
    batch_id = str(uuid4())
    service.staging.claim_manifest_finalization(manifest.id)
    service.staging.record_manifest_finalization(manifest.id, batch_id)
    batch_root = service.staging.root / "batches" / batch_id
    batch_root.mkdir(parents=True)
    (batch_root / "orphan.ready").write_bytes(payload)
    old = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    for path in (
        service.staging.root / "manifests" / manifest.id,
        service.staging.root / "manifests" / f"{manifest.id}.claim",
        service.staging.root / "manifests" / f"{manifest.id}.finalized",
    ):
        os.utime(path, (old, old))

    assert service.collect_staging(datetime.now(UTC)) == 2
    assert not batch_root.exists()
    assert not (service.staging.root / "manifests" / manifest.id).exists()
    assert service.staging.finalized_manifest_outcome(manifest.id) is None


def test_expired_pending_manifest_marks_postcommit_batch_finalized(tmp_path: Path) -> None:
    repository, service, _ = _prepared(tmp_path)
    payload = b"postcommit manifest"
    manifest = service.staging.begin_manifest(
        UploadKind.TRANSCRIPTS,
        [
            UploadManifestSlot(
                "00000000-0000-0000-0000-000000000005",
                "postcommit.txt",
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )
        ],
    )
    batch_id = str(uuid4())
    repository.create_batch(UploadKind.TRANSCRIPTS, batch_id)
    batch_root = service.staging.root / "batches" / batch_id
    batch_root.mkdir(parents=True)
    ready = batch_root / "referenced.ready"
    ready.write_bytes(payload)
    repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(
            batch_id,
            "referenced",
            ready,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "referenced.txt",
        ),
    )
    service.staging.claim_manifest_finalization(manifest.id)
    service.staging.record_manifest_finalization(manifest.id, batch_id)
    old = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    for path in (
        service.staging.root / "manifests" / manifest.id,
        service.staging.root / "manifests" / f"{manifest.id}.claim",
        service.staging.root / "manifests" / f"{manifest.id}.finalized",
    ):
        os.utime(path, (old, old))

    assert service.collect_staging(datetime.now(UTC)) == 1
    assert ready.exists()
    assert service.staging.finalized_manifest_outcome(manifest.id) == {
        "batch_id": batch_id
    }


def test_matching_evidence_decodes_complete_utf8_before_text_truncation(tmp_path: Path) -> None:
    _, service, _ = _prepared(tmp_path)
    path = tmp_path / "boundary.txt"
    path.write_bytes((b"a" * 8191) + "€ transcript".encode())

    title, opening = service._transcript_text(path)

    assert title.startswith("a")
    assert opening
    path.write_bytes(b"H\x82art failure")
    with pytest.raises(UploadRejected, match="not UTF-8"):
        service._transcript_text(path)


def test_expired_temporary_manifest_is_collected(tmp_path: Path) -> None:
    _, service, _ = _prepared(tmp_path)
    payload = b"manifest text"
    manifest = service.staging.begin_manifest(
        UploadKind.TRANSCRIPTS,
        [
            UploadManifestSlot(
                "00000000-0000-0000-0000-000000000001",
                "manifest.txt",
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )
        ],
    )
    root = service.staging.root / "manifests" / manifest.id
    old = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    os.utime(root, (old, old))

    assert service.collect_staging(datetime.now(UTC)) == 1
    assert not root.exists()


def test_expired_finalize_claims_and_orphan_claims_are_collected_safely(
    tmp_path: Path,
) -> None:
    _, service, _ = _prepared(tmp_path)
    payload = b"claimed manifest text"
    manifest = service.staging.begin_manifest(
        UploadKind.TRANSCRIPTS,
        [
            UploadManifestSlot(
                "00000000-0000-0000-0000-000000000002",
                "claimed.txt",
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )
        ],
    )
    service.staging.claim_manifest_finalization(manifest.id)
    root = service.staging.root / "manifests" / manifest.id
    claim = service.staging.root / "manifests" / f"{manifest.id}.claim"
    orphan_id = "00000000-0000-0000-0000-000000000003"
    orphan = service.staging.root / "manifests" / f"{orphan_id}.claim"
    orphan.write_text("finalize", encoding="ascii")
    old = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    os.utime(root, (old, old))
    os.utime(orphan, (old, old))

    # The still-fresh claim is a live lease: an old manifest alone is not
    # enough for expiry to steal a finalizer's ownership.
    assert service.collect_staging(datetime.now(UTC)) == 1
    assert root.exists()
    assert claim.exists()
    assert not orphan.exists()

    os.utime(claim, (old, old))
    assert service.collect_staging(datetime.now(UTC)) == 1
    assert not root.exists()
    assert not claim.exists()
    assert service.collect_staging(datetime.now(UTC)) == 0


def test_cleanup_keeps_active_paths_and_removes_old_complete_paths(tmp_path: Path) -> None:
    repository, service, lecture_id = _prepared(tmp_path)
    batch_id = repository.create_batch(UploadKind.TRANSCRIPTS)
    active_path = _add_item(
        repository, service.staging.root, batch_id, "active", lecture_id
    )
    complete_path = _add_item(
        repository, service.staging.root, batch_id, "complete", lecture_id
    )
    quarantined_path = _add_item(
        repository, service.staging.root, batch_id, "quarantined", lecture_id
    )
    review_path = _add_item(
        repository, service.staging.root, batch_id, "review", lecture_id
    )
    with repository.database.session() as session:
        complete = session.get(UploadItemModel, "complete")
        quarantined = session.get(UploadItemModel, "quarantined")
        review = session.get(UploadItemModel, "review")
        assert complete is not None
        assert quarantined is not None
        assert review is not None
        complete.state = UploadState.COMPLETE.value
        quarantined.state = UploadState.QUARANTINED.value
        review.state = UploadState.NEEDS_REVIEW.value
        complete.updated_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        quarantined.updated_at = complete.updated_at
        review.updated_at = complete.updated_at
        session.add(
            StudyRevisionModel(
                upload_item_id="review",
                lecture_id=lecture_id,
                kind=UploadKind.TRANSCRIPTS.value,
                source_sha256="f" * 64,
                immutable_source_path="",
                state="proposed",
                current=False,
            )
        )

    removed = service.collect_staging(datetime.now(UTC))

    assert removed == 1
    assert active_path.exists()
    assert not complete_path.exists()
    assert quarantined_path.exists()
    assert review_path.exists()
