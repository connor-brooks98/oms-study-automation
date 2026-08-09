import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.models import IngestionJobModel, UploadBatchModel, UploadItemModel


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
