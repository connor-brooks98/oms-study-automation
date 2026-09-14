from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader

from oms_hub.db import Database
from oms_hub.models import PublishedQuizMediaModel, PublishedQuizModel
from oms_hub.public_boundary import classify_public_path
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.study_generation.domain import GenerationKind, QuizImageRef
from oms_hub.study_generation.native_quiz import serialize_native_quiz
from oms_hub.study_generation.quiz_images import sanitize_quiz_image
from oms_hub.study_generation.repository import GenerationRepository
from oms_hub.web import published_quiz_exports as exports
from oms_hub.web.public_quiz_routes import router as public_router
from tests.study_generation.test_gpt_export_routes import export_client as export_client
from tests.study_generation.test_gpt_lecture import gpt_review_run as gpt_review_run
from tests.v2.test_public_quiz_routes import _published_app, _quiz


@pytest.fixture
def setup(tmp_path):
    with Database(f"sqlite:///{tmp_path / 'exports.db'}") as database:
        database.migrate()
        app = FastAPI()
        app.state.settings = SimpleNamespace(data_dir=tmp_path)
        app.state.database = database
        app.state.generation_repository = GenerationRepository(database)
        app.state.catalog_repository = CatalogRepository(database)
        app.state.owner = "owner"

        @app.middleware("http")
        async def identify(request: Request, call_next):
            if app.state.owner:
                request.state.study_owner_id = app.state.owner
            return await call_next(request)

        app.include_router(public_router)
        app.include_router(exports.router)

        def publish(title, course="Neuro", exam=2, lecture=1):
            identity = app.state.catalog_repository.upsert_lecture(
                LectureInput(course, exam, lecture, title, "", None)
            )
            job = app.state.generation_repository.queue(identity, GenerationKind.QUIZ)
            return app.state.generation_repository.publish_quiz(identity, job.id, _quiz(title))

        with TestClient(app) as client:
            yield client, app, publish, tmp_path


def pdf_text(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    reader = PdfReader(BytesIO(response.content))
    assert len(reader.pages) >= 3
    return "\n".join(page.extract_text() for page in reader.pages)


def test_public_single_preserves_legacy_questions_answers_and_publication_provenance(setup):
    client, app, publish, root = setup
    quiz = publish("Legacy retained quiz")
    app.state.owner = None
    response = client.get(f"/public/quizzes/{quiz.token}/export.pdf")
    text = pdf_text(response)
    (root / "public-quiz.pdf").write_bytes(response.content)
    assert "Legacy retained quiz" in text and "Which mechanism causes an aplastic crisis?" in text
    assert "Parvovirus B19 infects erythroid precursor cells." in text
    assert "Correct answer: A." in text and quiz.token in text
    assert "Missing legacy source references" in text
    assert "Accepted payload" not in text
    assert "attachment;" in response.headers["content-disposition"]
    assert client.get("/public/quizzes/not-a-token/export.pdf").status_code == 422
    assert client.get(f"/public/quizzes/{'0' * 64}/export.pdf").status_code == 404


def test_exam_bundle_exact_scope_category_order_and_auth(setup):
    client, app, publish, _ = setup
    first = publish("First quiz", lecture=1)
    second = publish("Second quiz", lecture=2)
    other = publish("Other course", course="Heme")
    publish("Other exam", exam=3)
    practice = publish("Practice category", lecture=3)
    with app.state.database.session() as session:
        session.get(PublishedQuizModel, first.token).display_order = 2
        session.get(PublishedQuizModel, second.token).display_order = 1
        session.get(PublishedQuizModel, practice.token).content_kind = "practice_questions"
        # Scope filtering occurs before parsing an unrelated incompatible payload.
        session.get(PublishedQuizModel, other.token).payload_json = '{"unsupported":true}'
    url = "/studio/library/exports/pdf?course=NEURO&exam=2&category=quizzes"
    text = pdf_text(client.get(url))
    assert text.index("Second quiz") < text.index("First quiz")
    assert (
        "Other course" not in text and "Other exam" not in text and "Practice category" not in text
    )
    practice_text = pdf_text(
        client.get(url.replace("category=quizzes", "category=practice_questions"))
    )
    assert "Practice category" in practice_text and "First quiz" not in practice_text
    assert client.get(url.replace("category=quizzes", "category=anything")).status_code == 422
    assert client.get(url.replace("exam=2", "exam=0")).status_code == 422
    assert client.get(url.replace("course=NEURO", "course=Unknown")).status_code == 404
    app.state.owner = None
    assert client.get(url).status_code == 401


def test_incompatible_member_fails_entire_bundle_without_silent_omission(setup):
    client, app, publish, _ = setup
    publish("Valid member")
    broken = publish("Incompatible member", lecture=2)
    with app.state.database.session() as session:
        session.get(PublishedQuizModel, broken.token).payload_json = '{"questions":[]}'
    response = client.get("/studio/library/exports/pdf?course=Neuro&exam=2&category=quizzes")
    assert response.status_code == 409
    assert "No quizzes were omitted" in response.json()["detail"]
    assert not response.content.startswith(b"%PDF")


def test_published_media_is_embedded_and_changed_file_fails_closed(setup, monkeypatch):
    client, app, publish, root = setup
    published = publish("Image quiz")
    buffer = BytesIO()
    Image.new("RGB", (80, 60), "navy").save(buffer, format="PNG")
    image = sanitize_quiz_image(buffer.getvalue())
    path = root / "published-image.png"
    path.write_bytes(image.payload)
    native = replace(
        published.quiz,
        questions=(
            replace(
                published.quiz.questions[0],
                image_ref=QuizImageRef(
                    "figure", "Lecture slides", "Slide 4", "Original lecture figure"
                ),
            ),
        ),
    )
    with app.state.database.session() as session:
        session.get(PublishedQuizModel, published.token).payload_json = serialize_native_quiz(
            native
        )
        session.add(
            PublishedQuizMediaModel(
                quiz_token=published.token,
                image_key="figure",
                path=str(path),
                sha256=image.sha256,
                media_type="image/png",
                width=image.width,
                height=image.height,
                alt_text="Original lecture figure",
            )
        )
    url = f"/public/quizzes/{published.token}/export.pdf"
    response = client.get(url)
    assert "Lecture slides, Slide 4" in pdf_text(response)
    reader = PdfReader(BytesIO(response.content))
    assert sum(len(page.images) for page in reader.pages) == 1
    render = exports.render_published_quiz_pdf

    def changed(*args, **kwargs):
        result = render(*args, **kwargs)
        path.write_bytes(b"changed")
        return result

    monkeypatch.setattr(exports, "render_published_quiz_pdf", changed)
    assert client.get(url).status_code == 409


def test_mutated_payload_or_membership_after_render_is_not_returned(setup, monkeypatch):
    client, app, publish, _ = setup
    published = publish("Before")
    render = exports.render_published_quiz_pdf

    def changed(*args, **kwargs):
        result = render(*args, **kwargs)
        app.state.generation_repository.rename_published_quiz(published.token, "After")
        return result

    monkeypatch.setattr(exports, "render_published_quiz_pdf", changed)
    assert client.get(f"/public/quizzes/{published.token}/export.pdf").status_code == 409

    def membership(*args, **kwargs):
        result = render(*args, **kwargs)
        publish("Added during export", lecture=2)
        return result

    monkeypatch.setattr(exports, "render_published_quiz_pdf", membership)
    assert (
        client.get("/studio/library/exports/pdf?course=Neuro&exam=2&category=quizzes").status_code
        == 409
    )


def test_only_exact_public_pdf_surface_is_anonymous():
    token = "a" * 64
    path = f"/public/quizzes/{token}/export.pdf"
    assert classify_public_path(path).is_public
    assert classify_public_path(path).category == "outline"
    for invalid in [
        path + "/extra",
        path.replace("export.pdf", "exports/pdf"),
        "/public/quizzes/bad/export.pdf",
        "/studio/library/exports/pdf",
    ]:
        assert not classify_public_path(invalid).is_public


def test_gpt_bundle_uses_owner_accepted_provenance_and_public_pdf_stays_public(
    export_client, monkeypatch
):
    client, app, run, _ = export_client
    app.include_router(exports.router)
    app.include_router(public_router)
    for question in app.state.practice_review.review(run.id):
        app.state.practice_review.verify_generated_answer(run.id, question.draft.question_id)
    published = app.state.generation_repository.publish_reviewed_studio_quiz(run.id)
    url = "/studio/library/exports/pdf?course=Heme&exam=3&category=quizzes"
    response = client.get(url)
    text = pdf_text(response)
    assert "Accepted payload SHA-256" in text
    assert "Fourth independent patient case." in text
    assert sum(len(page.images) for page in PdfReader(BytesIO(response.content)).pages) > 0
    # Every accepted rationale, including distractor explanations, survives export.
    for question in published.quiz.questions:
        assert " ".join(question.rationale.split()) in " ".join(text.split())
    app.state.owner = "other"
    assert client.get(url).status_code == 403
    app.state.owner = None
    assert client.get(url).status_code == 401

    def private_provenance_forbidden(*args):
        raise AssertionError("Public publication export must not read private review artifacts")

    monkeypatch.setattr(exports, "_snapshot", private_provenance_forbidden)
    public_text = pdf_text(client.get(f"/public/quizzes/{published.token}/export.pdf"))
    assert "Accepted payload" not in public_text
    assert "Published payload SHA-256" in public_text


def test_gpt_bundle_rechecks_original_source_after_render(export_client, monkeypatch):
    client, app, run, inputs = export_client
    app.include_router(exports.router)
    for question in app.state.practice_review.review(run.id):
        app.state.practice_review.verify_generated_answer(run.id, question.draft.question_id)
    app.state.generation_repository.publish_reviewed_studio_quiz(run.id)
    render = exports.export_reviewed_quiz

    def change_source(*args, **kwargs):
        result = render(*args, **kwargs)
        inputs.bindings[0].snapshot.path.write_bytes(b"changed during bundle render")
        return result

    monkeypatch.setattr(exports, "export_reviewed_quiz", change_source)
    response = client.get("/studio/library/exports/pdf?course=Heme&exam=3&category=quizzes")
    assert response.status_code == 409
    assert "No quizzes were omitted" in response.text
    assert not response.content.startswith(b"%PDF")


def test_real_app_routes_keep_public_single_private_bundle_and_scoped_links(tmp_path):
    app, published = _published_app(tmp_path, public=True)
    url = "/studio/library/exports/pdf?course=Neuro&exam=1&category=quizzes"
    with TestClient(app, base_url="https://study.example.com") as public:
        assert public.get(f"/public/quizzes/{published.token}/export.pdf").status_code == 200
        denied = public.get(url)
        assert denied.status_code == 503
        assert denied.json()["detail"] == "Cloudflare Access is not configured"
        library = public.get("/public/quizzes").text
        assert "/studio/library/exports/pdf" not in library
        assert "Download quiz PDF" in public.get(f"/public/quizzes/{published.token}").text
    with TestClient(app, base_url="http://127.0.0.1") as owner:
        assert owner.get(url).status_code == 200
        library = owner.get("/public/quizzes").text
        assert "/studio/library/exports/pdf?course=neuro&amp;exam=1&amp;category=quizzes" in library
