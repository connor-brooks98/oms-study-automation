import json
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from oms_hub.study_generation.native_quiz import serialize_native_quiz
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.web.gpt_export_routes import router
from tests.study_generation.test_gpt_lecture import gpt_review_run as gpt_review_run


@pytest.fixture
def export_client(gpt_review_run, tmp_path):
    repository, run, worker, _, inputs = gpt_review_run
    worker.run(run)
    app = FastAPI()
    app.state.settings = SimpleNamespace(data_dir=tmp_path)
    app.state.studio_repository = repository
    app.state.practice_review = PracticeReviewService(repository, worker.image_service,
        lecture_validator=worker.validate_review)
    app.state.generation_repository = GenerationRepository(repository.database,
        practice_review=app.state.practice_review)
    app.state.owner = "owner"

    @app.middleware("http")
    async def identify(request: Request, call_next):
        if app.state.owner:
            request.state.study_owner_id = app.state.owner
        return await call_next(request)

    app.include_router(router)
    with TestClient(app) as client:
        yield client, app, run, inputs


def test_exports_require_owner_review_and_share_published_payload(export_client):
    client, app, run, _ = export_client
    path = f"/studio/runs/{run.id}/exports/"
    assert client.get(path + "json").status_code == 409
    for question in app.state.practice_review.review(run.id):
        app.state.practice_review.verify_generated_answer(run.id, question.draft.question_id)
    raw = client.get(path + "json")
    assert raw.status_code == 200 and raw.headers["cache-control"] == "private, no-store"
    zipped = client.get(path + "zip")
    with ZipFile(BytesIO(zipped.content)) as archive:
        assert archive.read("quiz.json") == raw.content
        manifest = json.loads(archive.read("manifest.json"))
        assert set(manifest["provenance"]["questions"]) == {"q1", "q2", "q3", "q4"}
        assert all(row["source_refs"] and row["objective_ids"]
                   for row in manifest["provenance"]["questions"].values())
    assert client.get(path + "pdf").content.startswith(b"%PDF-")
    published = app.state.generation_repository.publish_reviewed_studio_quiz(run.id)
    assert client.get(path + "json").content == serialize_native_quiz(published.quiz).encode()
    app.state.owner = "other"
    assert client.get(path + "json").status_code == 403
    app.state.owner = None
    assert client.get(path + "json").status_code == 401


def test_export_rechecks_sources_after_rendering(export_client, monkeypatch):
    from oms_hub.study_generation import quiz_export

    client, app, run, inputs = export_client
    for question in app.state.practice_review.review(run.id):
        app.state.practice_review.verify_generated_answer(run.id, question.draft.question_id)
    export = quiz_export.export_reviewed_quiz

    def change_source(*args, **kwargs):
        result = export(*args, **kwargs)
        inputs.bindings[0].snapshot.path.write_bytes(b"changed during render")
        return result

    monkeypatch.setattr(quiz_export, "export_reviewed_quiz", change_source)
    response = client.get(f"/studio/runs/{run.id}/exports/pdf")
    assert response.status_code == 409 and "Review current lecture sources" in response.text
