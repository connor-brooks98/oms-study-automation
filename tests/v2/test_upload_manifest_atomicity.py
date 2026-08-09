import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.ingestion.staging import UploadRejected
from oms_hub.models import IngestionJobModel, UploadBatchModel, UploadItemModel
from oms_hub.repositories import LectureInput


def _client(tmp_path: Path) -> tuple[TestClient, object]:
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            allow_local_access=True,
        )
    )
    return TestClient(app), app


def _counts(app: object) -> tuple[int, int, int]:
    with app.state.database.session() as session:  # type: ignore[attr-defined]
        return tuple(
            int(session.scalar(select(func.count()).select_from(model)) or 0)
            for model in (UploadBatchModel, UploadItemModel, IngestionJobModel)
        )


def _ready(app: object) -> list[Path]:
    root = app.state.upload_staging.root / "batches"  # type: ignore[attr-defined]
    return list(root.rglob("*.ready")) if root.exists() else []


def test_multipart_manifest_is_atomic_when_valid_precedes_invalid(tmp_path: Path) -> None:
    client, app = _client(tmp_path)

    response = client.post(
        "/uploads/transcripts",
        files=[
            ("files", ("valid.txt", b"Valid UTF-8 transcript.")),
            ("files", ("invalid.pdf", b"not a transcript")),
        ],
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["filename"] == "invalid.pdf"
    assert _counts(app) == (0, 0, 0)
    assert _ready(app) == []


def test_multipart_manifest_is_atomic_when_invalid_precedes_valid(tmp_path: Path) -> None:
    client, app = _client(tmp_path)

    response = client.post(
        "/uploads/transcripts",
        files=[
            ("files", ("invalid.pdf", b"not a transcript")),
            ("files", ("valid.txt", b"Valid UTF-8 transcript.")),
        ],
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["filename"] == "invalid.pdf"
    assert _counts(app) == (0, 0, 0)
    assert _ready(app) == []


def test_cp1252_transcript_is_rejected_before_any_job_is_created(tmp_path: Path) -> None:
    client, app = _client(tmp_path)

    response = client.post(
        "/uploads/transcripts",
        files=[("files", ("legacy.txt", b"H\x82art failure transcript"))],
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["detail"] == "transcript is not UTF-8"
    assert _counts(app) == (0, 0, 0)
    assert _ready(app) == []


def test_manifest_descriptor_rejection_is_structured_and_not_durable(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    slot_id = str(uuid4())

    response = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [
                {
                    "slot_id": slot_id,
                    "filename": "wrong.pdf",
                    "size_bytes": 4,
                    "sha256": hashlib.sha256(b"nope").hexdigest(),
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {
            "slot_id": slot_id,
            "filename": "wrong.pdf",
            "code": "validation_failed",
            "detail": "transcripts uploads require .txt",
        }
    ]
    assert _counts(app) == (0, 0, 0)


def test_mixed_multipart_and_chunk_slots_finalize_one_parent_batch(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    small = b"Small UTF-8 transcript."
    large = b"Large UTF-8 transcript."
    small_slot = str(uuid4())
    large_slot = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [
                {
                    "slot_id": small_slot,
                    "filename": "small.txt",
                    "size_bytes": len(small),
                    "sha256": hashlib.sha256(small).hexdigest(),
                },
                {
                    "slot_id": large_slot,
                    "filename": "large.txt",
                    "size_bytes": len(large),
                    "sha256": hashlib.sha256(large).hexdigest(),
                },
            ],
        },
    )
    assert created.status_code == 201
    manifest_id = created.json()["manifest_id"]

    multipart = client.post(
        "/uploads/transcripts",
        data={"manifest_id": manifest_id, "slot_ids": small_slot},
        files=[("files", ("small.txt", small))],
    )
    assert multipart.status_code == 202
    assert multipart.json() == {"manifest_id": manifest_id}
    assert _counts(app) == (0, 0, 0)

    chunk = client.post(
        "/api/upload-chunks",
        json={
            "kind": "transcripts",
            "filename": "large.txt",
            "total_size": len(large),
            "sha256": hashlib.sha256(large).hexdigest(),
            "manifest_id": manifest_id,
            "slot_id": large_slot,
        },
    )
    assert chunk.status_code == 201
    session_id = chunk.json()["session_id"]
    appended = client.put(
        f"/api/upload-chunks/{session_id}?offset=0", content=large
    )
    assert appended.status_code == 200
    assert client.post(f"/api/upload-chunks/{session_id}/finalize").status_code == 202

    finalized = client.post(f"/api/upload-manifests/{manifest_id}/finalize")
    assert finalized.status_code == 202
    batch = client.get(f"/api/upload-batches/{finalized.json()['batch_id']}")
    assert batch.status_code == 200
    assert len(batch.json()["items"]) == 2
    assert _counts(app) == (1, 2, 0)


def test_cancelled_manifest_chunk_can_never_fall_back_to_legacy_batch(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    payload = b"cancelled transcript"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [{
                "slot_id": slot_id,
                "filename": "cancelled.txt",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }],
        },
    )
    manifest_id = created.json()["manifest_id"]
    session = client.post(
        "/api/upload-chunks",
        json={
            "kind": "transcripts",
            "filename": "cancelled.txt",
            "total_size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "manifest_id": manifest_id,
            "slot_id": slot_id,
        },
    )
    session_id = session.json()["session_id"]
    assert client.delete(f"/api/upload-manifests/{manifest_id}").status_code == 204

    stale = client.post(f"/api/upload-chunks/{session_id}/finalize")

    assert stale.status_code == 422
    assert _counts(app) == (0, 0, 0)
    assert _ready(app) == []


def test_chunk_session_rejects_partial_manifest_ownership_pair(tmp_path: Path) -> None:
    client, _app = _client(tmp_path)
    payload = b"partial manifest pair"
    request = {
        "kind": "transcripts",
        "filename": "partial.txt",
        "total_size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }

    only_manifest = client.post(
        "/api/upload-chunks",
        json={**request, "manifest_id": str(uuid4())},
    )
    only_slot = client.post(
        "/api/upload-chunks",
        json={**request, "slot_id": str(uuid4())},
    )

    assert only_manifest.status_code == 422
    assert only_slot.status_code == 422
    assert only_manifest.json()["detail"] == "manifest_id and slot_id must be supplied together"


def test_manifest_delete_after_chunk_staging_never_uses_legacy_finalize(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    client, app = _client(tmp_path)
    payload = b"manifest finalization race"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [{
                "slot_id": slot_id,
                "filename": "race.txt",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }],
        },
    )
    manifest_id = created.json()["manifest_id"]
    started = client.post(
        "/api/upload-chunks",
        json={
            "kind": "transcripts",
            "filename": "race.txt",
            "total_size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "manifest_id": manifest_id,
            "slot_id": slot_id,
        },
    )
    session_id = started.json()["session_id"]
    appended = client.put(
        f"/api/upload-chunks/{session_id}?offset=0", content=payload
    )
    assert appended.status_code == 200

    staging = app.state.upload_staging  # type: ignore[attr-defined]
    finalize = staging.finalize_chunks

    def finalize_then_delete(session_id: str) -> object:
        staged = finalize(session_id)
        staging.discard_manifest(staged.batch_id)
        return staged

    monkeypatch.setattr(staging, "finalize_chunks", finalize_then_delete)
    raced = client.post(f"/api/upload-chunks/{session_id}/finalize")

    assert raced.status_code == 422
    assert _counts(app) == (0, 0, 0)
    assert _ready(app) == []


def test_cancel_before_manifest_finalize_leaves_no_durable_rows_or_ready_files(
    tmp_path: Path,
) -> None:
    client, app = _client(tmp_path)
    payload = b"cancel before finalization"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [{
                "slot_id": slot_id,
                "filename": "before.txt",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }],
        },
    )
    manifest_id = created.json()["manifest_id"]
    uploaded = client.post(
        "/uploads/transcripts",
        data={"manifest_id": manifest_id, "slot_ids": slot_id},
        files=[("files", ("before.txt", payload))],
    )
    assert uploaded.status_code == 202

    assert client.delete(f"/api/upload-manifests/{manifest_id}").status_code == 204
    finalized = client.post(f"/api/upload-manifests/{manifest_id}/finalize")

    assert finalized.status_code == 422
    assert _counts(app) == (0, 0, 0)
    assert _ready(app) == []


def test_finalize_claim_rejects_cancel_during_move_and_reverts_without_orphans(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    client, app = _client(tmp_path)
    payload = b"cancel while promoting"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [{
                "slot_id": slot_id,
                "filename": "during.txt",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }],
        },
    )
    manifest_id = created.json()["manifest_id"]
    uploaded = client.post(
        "/uploads/transcripts",
        data={"manifest_id": manifest_id, "slot_ids": slot_id},
        files=[("files", ("during.txt", payload))],
    )
    assert uploaded.status_code == 202

    staging = app.state.upload_staging  # type: ignore[attr-defined]
    promote = staging.promote_manifest

    def promote_while_cancel_is_rejected(
        manifest_id: str, batch_id: str
    ) -> object:
        with pytest.raises(UploadRejected, match="is finalizing"):
            staging.discard_manifest(manifest_id)
        return promote(manifest_id, batch_id)

    def fail_finalization(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("database finalization failed")

    monkeypatch.setattr(staging, "promote_manifest", promote_while_cancel_is_rejected)
    monkeypatch.setattr(app.state.ingestion_repository, "finalize_batch", fail_finalization)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="database finalization failed"):
        client.post(f"/api/upload-manifests/{manifest_id}/finalize")

    assert _counts(app) == (0, 0, 0)
    assert _ready(app) == []
    assert client.delete(f"/api/upload-manifests/{manifest_id}").status_code == 204


def test_finalize_owned_manifest_returns_conflict_to_cancel_request(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    payload = b"finalizer owns cancellation"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [{
                "slot_id": slot_id,
                "filename": "owned.txt",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }],
        },
    )
    manifest_id = created.json()["manifest_id"]
    staging = app.state.upload_staging  # type: ignore[attr-defined]
    staging.claim_manifest_finalization(manifest_id)

    cancelled = client.delete(f"/api/upload-manifests/{manifest_id}")

    assert cancelled.status_code == 409
    assert cancelled.json()["detail"] == "upload manifest is finalizing"
    staging.release_manifest_finalization(manifest_id)


def test_matching_failure_releases_finalize_claim_and_preserves_retry_data(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    client, app = _client(tmp_path)
    payload = b"matching failure retains manifest"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [{
                "slot_id": slot_id,
                "filename": "matching.txt",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }],
        },
    )
    manifest_id = created.json()["manifest_id"]
    uploaded = client.post(
        "/uploads/transcripts",
        data={"manifest_id": manifest_id, "slot_ids": slot_id},
        files=[("files", ("matching.txt", payload))],
    )
    assert uploaded.status_code == 202

    def fail_match(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("matching unavailable")

    monkeypatch.setattr(app.state.ingestion_service, "decide_staged", fail_match)  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="matching unavailable"):
        client.post(f"/api/upload-manifests/{manifest_id}/finalize")

    staging = app.state.upload_staging  # type: ignore[attr-defined]
    assert (staging.root / "manifests" / manifest_id).is_dir()
    assert not (staging.root / "manifests" / f"{manifest_id}.claim").exists()
    assert _counts(app) == (0, 0, 0)


def test_manifest_preflight_exception_releases_claim_and_preserves_retry_data(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    client, app = _client(tmp_path)
    payload = b"preflight exception retains manifest"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [{
                "slot_id": slot_id,
                "filename": "preflight.txt",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }],
        },
    )
    manifest_id = created.json()["manifest_id"]
    uploaded = client.post(
        "/uploads/transcripts",
        data={"manifest_id": manifest_id, "slot_ids": slot_id},
        files=[("files", ("preflight.txt", payload))],
    )
    assert uploaded.status_code == 202
    staging = app.state.upload_staging  # type: ignore[attr-defined]

    def fail_preflight(*_args: object, **_kwargs: object) -> object:
        raise OSError("staging device unavailable")

    monkeypatch.setattr(staging, "manifest_uploads", fail_preflight)
    with pytest.raises(OSError, match="staging device unavailable"):
        client.post(f"/api/upload-manifests/{manifest_id}/finalize")

    assert (staging.root / "manifests" / manifest_id).is_dir()
    assert not (staging.root / "manifests" / f"{manifest_id}.claim").exists()
    assert _counts(app) == (0, 0, 0)


def test_post_commit_cancel_returns_authoritative_finalized_batch(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    payload = b"committed manifest outcome"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [{
                "slot_id": slot_id,
                "filename": "committed.txt",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }],
        },
    )
    manifest_id = created.json()["manifest_id"]
    uploaded = client.post(
        "/uploads/transcripts",
        data={"manifest_id": manifest_id, "slot_ids": slot_id},
        files=[("files", ("committed.txt", payload))],
    )
    assert uploaded.status_code == 202
    finalized = client.post(f"/api/upload-manifests/{manifest_id}/finalize")
    assert finalized.status_code == 202

    cancelled = client.delete(f"/api/upload-manifests/{manifest_id}")

    assert cancelled.status_code == 409
    assert cancelled.json()["batch_id"] == finalized.json()["batch_id"]
    assert _counts(app) == (1, 1, 0)


def test_pending_finalization_outcome_rejects_cancel_without_false_success(
    tmp_path: Path,
) -> None:
    client, app = _client(tmp_path)
    payload = b"pending manifest outcome"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={
            "kind": "transcripts",
            "files": [{
                "slot_id": slot_id,
                "filename": "pending.txt",
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }],
        },
    )
    manifest_id = created.json()["manifest_id"]
    staging = app.state.upload_staging  # type: ignore[attr-defined]
    staging.claim_manifest_finalization(manifest_id)
    staging.record_manifest_finalization(manifest_id, str(uuid4()))
    staging._discard_manifest_data(manifest_id)
    staging.release_manifest_finalization(manifest_id)

    cancelled = client.delete(f"/api/upload-manifests/{manifest_id}")

    assert cancelled.status_code == 409
    assert cancelled.json()["detail"] == "upload manifest finalization is unresolved"
    assert _counts(app) == (0, 0, 0)


def test_postcommit_cleanup_failure_still_returns_accepted_batch(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    client, app = _client(tmp_path)
    payload = b"accepted despite cleanup fault"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={"kind": "transcripts", "files": [{
            "slot_id": slot_id, "filename": "cleanup.txt", "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }]},
    )
    manifest_id = created.json()["manifest_id"]
    assert client.post(
        "/uploads/transcripts",
        data={"manifest_id": manifest_id, "slot_ids": slot_id},
        files=[("files", ("cleanup.txt", payload))],
    ).status_code == 202
    staging = app.state.upload_staging  # type: ignore[attr-defined]
    monkeypatch.setattr(
        staging, "complete_manifest_finalization",
        lambda *_args: (_ for _ in ()).throw(OSError("cleanup unavailable")),
    )

    finalized = client.post(f"/api/upload-manifests/{manifest_id}/finalize")

    assert finalized.status_code == 202
    assert _counts(app) == (1, 1, 0)
    assert staging._manifest_finalization_outcome(manifest_id) is not None
    old = (datetime.now(UTC) - timedelta(hours=25)).timestamp()
    for path in (
        staging.root / "manifests" / manifest_id,
        staging.root / "manifests" / f"{manifest_id}.claim",
        staging.root / "manifests" / f"{manifest_id}.finalized",
    ):
        os.utime(path, (old, old))
    app.state.ingestion_service.collect_staging(datetime.now(UTC))  # type: ignore[attr-defined]
    assert staging.finalized_manifest_outcome(manifest_id) == {
        "batch_id": finalized.json()["batch_id"]
    }


def test_postcommit_catalog_failure_still_returns_accepted_batch(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    client, app = _client(tmp_path)
    lecture_id = app.state.catalog_repository.upsert_lecture(  # type: ignore[attr-defined]
        LectureInput("Cardiology", 1, 8, "Arrhythmia", "Dr Test", None)
    )
    payload = b"accepted despite catalog fault"
    slot_id = str(uuid4())
    created = client.post(
        "/api/upload-manifests",
        json={"kind": "transcripts", "lecture_id": lecture_id, "files": [{
            "slot_id": slot_id, "filename": "catalog.txt", "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }]},
    )
    manifest_id = created.json()["manifest_id"]
    assert client.post(
        "/uploads/transcripts",
        data={"manifest_id": manifest_id, "slot_ids": slot_id},
        files=[("files", ("catalog.txt", payload))],
    ).status_code == 202
    monkeypatch.setattr(
        app.state.ingestion_service, "_complete_match_steps",  # type: ignore[attr-defined]
        lambda *_args: (_ for _ in ()).throw(RuntimeError("catalog unavailable")),
    )

    finalized = client.post(f"/api/upload-manifests/{manifest_id}/finalize")

    assert finalized.status_code == 202
    assert _counts(app) == (1, 1, 1)
