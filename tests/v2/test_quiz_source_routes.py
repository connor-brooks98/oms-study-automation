from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from oms_hub.models import PublishedQuizModel, StudioRunModel, StudyRevisionModel
from oms_hub.public_boundary import classify_public_path
from oms_hub.question_bank.repository import BankRepository
from oms_hub.study_generation.practice_review import PracticeReviewService
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.study_progress.sessions import AnswerSelection, StudySessionService
from oms_hub.web.quiz_source_routes import router
from tests.study_generation.test_gpt_lecture import gpt_review_run as gpt_review_run


@pytest.fixture
def source_client(gpt_review_run, tmp_path):
    repository, run, worker, _, inputs = gpt_review_run
    worker.run(run)
    review = PracticeReviewService(
        repository, worker.image_service, lecture_validator=worker.validate_review
    )
    for question in review.review(run.id):
        review.verify_generated_answer(run.id, question.draft.question_id)
    generation = GenerationRepository(repository.database, practice_review=review)
    publication = generation.publish_reviewed_studio_quiz(run.id)
    service = StudySessionService(
        repository.database.session,
        bank=BankRepository(repository.database.session),
        load_quiz=lambda owner, token: generation.published_quiz(token),
        topics_for=lambda owner, key: (),
        media_for=lambda owner, quiz: generation.published_quiz_media(quiz.token),
    )
    app = FastAPI()
    app.state.settings = SimpleNamespace(data_dir=tmp_path)
    app.state.studio_repository = repository
    app.state.practice_review = review
    app.state.generation_repository = generation
    app.state.gpt_lecture_service = SimpleNamespace(load_inputs=worker.load_inputs)
    app.state.study_session_service = service
    app.state.owner = "owner"

    @app.middleware("http")
    async def identify(request: Request, call_next):
        if app.state.owner:
            request.state.study_owner_id = app.state.owner
        return await call_next(request)

    app.include_router(router)
    view = service.create("owner", publication.token)
    attempt = view.questions[0]["attempt_id"]
    url = f"/study/sessions/{view.id}/sources/{attempt}"

    def answer():
        service.answer(
            view.id,
            attempt,
            AnswerSelection(choice_id=publication.quiz.questions[0].correct_choice_id),
            owner_id="owner",
        )

    with TestClient(app) as client:
        yield client, app, url, answer, inputs, publication, run


def test_only_owner_can_read_after_completed_grading(source_client):
    client, app, url, answer, inputs, _, _ = source_client
    assert client.get(url).status_code == 409
    assert client.get(url + "/0/original").status_code == 409
    answer()
    response = client.get(url)
    assert response.status_code == 200 and response.json()["available"]
    assert response.headers["cache-control"] == "private, no-store"
    sources = response.json()["sources"]
    assert sources[0]["excerpt"] and sources[0]["locator"]
    assert str(inputs.bindings[0].snapshot.path) not in response.text
    original = client.get(sources[0]["original_url"])
    assert original.content == inputs.bindings[0].snapshot.path.read_bytes()
    assert original.headers["x-content-type-options"] == "nosniff"
    app.state.owner = "other"
    assert client.get(url).status_code == 403
    assert client.get(url + "/0/original").status_code == 403
    app.state.owner = None
    assert client.get(url).status_code == 401
    assert client.get(url + "/0/original").status_code == 401


@pytest.mark.parametrize("defect", ["bytes", "revision", "version", "review", "index"])
def test_stale_evidence_fails_closed(source_client, defect):
    client, app, url, answer, inputs, publication, run = source_client
    answer()
    if defect == "bytes":
        inputs.bindings[0].snapshot.path.write_bytes(b"changed")
    else:
        with app.state.studio_repository.database.session() as session:
            if defect == "revision":
                session.get(StudyRevisionModel, inputs.bindings[0].revision_id).current = False
                session.get(StudyRevisionModel, inputs.bindings[0].revision_id).state = "superseded"
            if defect == "version":
                session.get(PublishedQuizModel, publication.token).version += 1
            if defect == "review":
                session.get(StudioRunModel, run.id).state = "failed"
    if defect == "index":
        assert client.get(url + "/-1/original").status_code == 409
        assert client.get(url + "/99/original").status_code == 409
    else:
        response = client.get(url)
        assert not response.json()["available"] and not response.json()["sources"]
        assert client.get(url + "/0/original").status_code == 409


def test_legacy_no_invented_source_and_no_public_route(source_client):
    client, app, url, answer, _, _, run = source_client
    answer()
    with app.state.studio_repository.database.session() as session:
        session.get(StudioRunModel, run.id).published_token = None
    assert client.get(url).json() == {
        "available": False,
        "message": "Source preview unavailable for this quiz.",
        "sources": [],
    }
    assert client.get(url + "/0/original").status_code == 409
    assert not classify_public_path(url).is_public
    assert not classify_public_path("/static/quiz_sources.js").is_public
    assert client.get("/public/quizzes/token/sources/question").status_code == 404


def test_attempt_cannot_be_moved_between_sessions(source_client):
    client, app, url, answer, _, publication, _ = source_client
    answer()
    other = app.state.study_session_service.create("owner", publication.token)
    changed = url.replace(url.split("/")[3], other.id)
    assert client.get(changed).status_code == 403


def test_change_during_original_read_is_not_returned(source_client, monkeypatch):
    from pathlib import Path

    client, _, url, answer, inputs, _, _ = source_client
    answer()
    original = Path.read_bytes

    def read(path):
        payload = original(path)
        if path == inputs.bindings[0].snapshot.path:
            path.write_bytes(b"replaced after read")
        return payload

    monkeypatch.setattr(Path, "read_bytes", read)
    assert client.get(url + "/0/original").status_code == 409


@pytest.mark.parametrize(
    "suffix,instructions", [(".pptx", "only red text"), (".pdf", "only pages 2–3")]
)
def test_scoped_citations_resolve_original_red_runs_or_pdf_pages(tmp_path, suffix, instructions):
    from oms_hub.ingestion.domain import UploadKind
    from oms_hub.repositories import CatalogRepository
    from oms_hub.slides.pipeline import SlidePipeline
    from oms_hub.study_generation.gpt_lecture import GptLectureWorker
    from oms_hub.study_generation.quiz_images import StudioQuizImageService
    from oms_hub.study_generation.service import GptLectureService
    from oms_hub.study_generation.studio_repository import StudioRepository
    from oms_hub.transcripts.pipeline import TranscriptPipeline
    from oms_hub.web.quiz_source_routes import _evidence
    from tests.study_generation.test_quiz_instructions import _router, _ScopedClient, _source
    from tests.v2.test_lecture_intake_formats import (
        _Cleaner,
        _Converter,
        _environment,
        _Prompt,
        _stage,
    )

    database, settings, ingestion, staging, _, lecture = _environment(tmp_path)
    material = _stage(ingestion, staging, _source(tmp_path, suffix), UploadKind.SLIDES, lecture)
    SlidePipeline(database, settings, _Converter()).process(material.item_id)
    spoken = tmp_path / "spoken.txt"
    spoken.write_text("Spoken excluded context.")
    transcript = _stage(ingestion, staging, spoken, UploadKind.TRANSCRIPTS, lecture)
    TranscriptPipeline(database, settings, _Prompt(), _Cleaner()).process(transcript.item_id)
    repository = StudioRepository(database)
    service = GptLectureService(
        CatalogRepository(database),
        ingestion,
        repository,
        _router(),
        None,
        settings.data_dir / "sources",
        owner_id="owner",
        model="gpt-5.5",
    )
    queued = service.queue(lecture, owner_id="owner", instructions=instructions)
    inputs = service.load_inputs(queued)
    fake = _ScopedClient(settings.data_dir / "work", inputs)
    images = StudioQuizImageService(repository, settings.data_dir / "media")
    worker = GptLectureWorker(
        repository, fake, service.load_inputs, "gpt-5.5", images, settings.data_dir / "evidence"
    )
    worker.run(repository.claim_next_run())
    review = PracticeReviewService(repository, images, lecture_validator=worker.validate_review)
    for question in review.review(queued.id):
        review.verify_generated_answer(queued.id, question.draft.question_id)
    generation = GenerationRepository(database, practice_review=review)
    publication = generation.publish_reviewed_studio_quiz(queued.id)
    sessions = StudySessionService(
        database.session,
        bank=BankRepository(database.session),
        load_quiz=lambda owner, token: generation.published_quiz(token),
        topics_for=lambda owner, key: (),
        media_for=lambda owner, quiz: (),
    )
    view = sessions.create("owner", publication.token)
    attempt = view.questions[0]["attempt_id"]
    sessions.answer(
        view.id,
        attempt,
        AnswerSelection(choice_id=publication.quiz.questions[0].correct_choice_id),
        owner_id="owner",
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                studio_repository=repository,
                practice_review=review,
                generation_repository=generation,
                settings=settings,
                study_session_service=sessions,
                gpt_lecture_service=service,
            )
        )
    )
    evidence = _evidence(request, view.id, attempt, "owner")
    assert evidence.rows
    if suffix == ".pptx":
        assert all(row["segment_key"].startswith("red-run-") for row in evidence.rows)
        assert all(
            "Red fact" in row["excerpt"] and "Black excluded" not in row["excerpt"]
            for row in evidence.rows
        )
        assert "slide" in evidence.rows[0]["locator"].lower()
        assert evidence.rows[0]["location"] == "Slide 1"
    else:
        from oms_hub.web.quiz_source_routes import source_original

        request.state = SimpleNamespace(study_owner_id="owner")
        request.headers = {}
        assert "#page=2" in evidence.rows[0]["original_url"]
        assert evidence.rows[0]["location"] == "Page 2"
        original = source_original(request, view.id, attempt, 0)
        assert original.media_type == "application/pdf"
        assert original.headers["content-disposition"].startswith("inline;")
        assert original.body == evidence.sources[0].path.read_bytes()
    assert evidence.sources[0].path.read_bytes() == inputs.bindings[0].snapshot.path.read_bytes()
    assert len(fake.requests) == 1  # The source preview never dispatches generation.
