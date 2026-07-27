from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.repositories import LectureInput
from oms_hub.study_generation.domain import GenerationKind
from oms_hub.study_generation.outline import OutlinePdfRenderer


def test_outline_route_rejects_changed_current_file(tmp_path):
    study_root = tmp_path / "study"
    path = study_root / "Neuro" / "Exam 1" / "Lecture Outlines" / "outline.pdf"
    path.parent.mkdir(parents=True)
    payload = OutlinePdfRenderer().render("Neuro Outline", "# Topic\nContent")
    path.write_bytes(payload)
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path / "data",
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            study_root=study_root,
        )
    )
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "Topic", "", None)
    )
    job = app.state.generation_repository.queue(
        lecture_id,
        GenerationKind.OUTLINE,
    )
    import hashlib

    record = app.state.generation_repository.record_outline(
        lecture_id,
        job.id,
        path,
        hashlib.sha256(payload).hexdigest(),
    )
    path.write_bytes(b"changed")

    response = TestClient(app).get(f"/artifacts/outlines/{record.id}")

    assert response.status_code == 409
