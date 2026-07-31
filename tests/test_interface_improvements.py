import hashlib

import pytest
from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.ingestion.domain import MatchDecision, StagedUpload, UploadKind, UploadState
from oms_hub.repositories import LectureInput
from tests.support import csrf_client


def _app(tmp_path):
    return create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        )
    )


def test_adjacent_lectures_roll_across_exams_but_not_courses(tmp_path):
    app = _app(tmp_path)
    catalog = app.state.catalog_repository
    first = catalog.upsert_lecture(LectureInput("Neuro", 1, 1, "A", "", None))
    second = catalog.upsert_lecture(LectureInput("Neuro", 2, 1, "B", "", None))
    catalog.upsert_lecture(LectureInput("Cardio", 1, 1, "C", "", None))

    previous, following = catalog.get_adjacent_lectures(first)
    assert previous is None
    assert following is not None and following.id == second
    previous, following = catalog.get_adjacent_lectures(second)
    assert previous is not None and previous.id == first
    assert following is None


def test_batch_quarantine_assignment_validates_every_item_before_mutation(tmp_path):
    app = _app(tmp_path)
    catalog = app.state.catalog_repository
    lecture_id = catalog.upsert_lecture(
        LectureInput("Neuro", 1, 1, "A", "", None)
    )
    repository = app.state.ingestion_repository
    batch_id = repository.create_batch(UploadKind.TRANSCRIPTS)
    for item_id in ("first", "second"):
        payload = item_id.encode()
        path = tmp_path / f"{item_id}.txt"
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
        repository.apply_match(
            item_id,
            MatchDecision("quarantined", None, 0, ("ambiguous",)),
        )

    with pytest.raises(KeyError):
        repository.assign_quarantined_items(["first", "missing"], lecture_id)
    assert repository.require_item("first").state is UploadState.QUARANTINED

    assigned = repository.assign_quarantined_items(
        ["first", "second"],
        lecture_id,
    )
    assert [item.state for item in assigned] == [UploadState.QUEUED] * 2
    assert repository.count_jobs("first", "process") == 1
    assert repository.count_jobs("second", "process") == 1


def test_legacy_upload_url_preserves_selected_lecture(tmp_path):
    app = _app(tmp_path)
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "A", "", None)
    )

    response = TestClient(app).get(
        f"/uploads/slides?lecture_id={lecture_id}",
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == f"/uploads?lecture_id={lecture_id}"


def test_quarantine_batch_route_returns_per_item_results(tmp_path):
    app = _app(tmp_path)
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "A", "", None)
    )
    repository = app.state.ingestion_repository
    batch_id = repository.create_batch(UploadKind.TRANSCRIPTS)
    payload = b"ambiguous"
    path = tmp_path / "ambiguous.txt"
    path.write_bytes(payload)
    repository.add_item(
        UploadKind.TRANSCRIPTS,
        StagedUpload(
            batch_id,
            "ambiguous",
            path,
            hashlib.sha256(payload).hexdigest(),
            len(payload),
            "ambiguous.txt",
        ),
    )
    repository.apply_match(
        "ambiguous",
        MatchDecision("quarantined", None, 0, ("ambiguous",)),
    )

    response = csrf_client(app).post(
        "/quarantine/assign",
        json={"item_ids": ["ambiguous"], "lecture_id": lecture_id},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [{"id": "ambiguous", "state": "queued"}]
    }
