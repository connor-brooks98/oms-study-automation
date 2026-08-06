import hashlib
import json

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.models import StudioRunModel
from oms_hub.repositories import LectureInput
from oms_hub.study_generation.domain import GenerationKind
from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.outline import OutlinePdfRenderer


def _quiz(title: str = "Lecture 1 Practice Quiz"):
    return parse_native_quiz(
        json.dumps(
            {
                "title": title,
                "questions": [
                    {
                        "stem": "Which mechanism causes an aplastic crisis?",
                        "choices": [
                            "Lysis of erythroid precursors",
                            "Immune-complex deposition",
                            "Destruction of mature red cells",
                            "Stem-cell transformation",
                        ],
                        "correct_index": 0,
                        "rationale": (
                            "Parvovirus B19 infects erythroid precursor cells."
                        ),
                    }
                ],
            }
        )
    )


def _published_app(tmp_path, *, public=False):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
        study_root=tmp_path / "study",
        public_hostname="study.example.com" if public else None,
    )
    app = create_app(settings)
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 1, "General CNS Pathology", "", None)
    )
    job = app.state.generation_repository.queue(
        lecture_id,
        GenerationKind.QUIZ,
    )
    published = app.state.generation_repository.publish_quiz(
        lecture_id,
        job.id,
        _quiz(),
    )
    return app, published


def _published_mixed_app(tmp_path, *, public=False):
    app, lecture_quiz = _published_app(tmp_path, public=public)
    with app.state.database.session() as session:
        session.add(
            StudioRunModel(
                id="practice-library-run",
                subject="Neuro",
                subject_key="neuro",
                exam_number=1,
                destination_subject="Neuro",
                destination_subject_key="neuro",
                destination_exam_number=1,
                label="Practice Questions",
                label_key="practice questions",
                prompt="",
                workflow_kind="direct_import",
                content_kind="practice_questions",
                state="awaiting_review",
                stage="review",
            )
        )
    practice = app.state.generation_repository.publish_studio_quiz(
        "practice-library-run",
        _quiz("Practice Questions"),
    )
    return app, lecture_quiz, practice


def test_public_library_groups_only_published_quizzes(tmp_path):
    app, published = _published_app(tmp_path)
    app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 2, "Unpublished lecture", "", None)
    )

    response = TestClient(app).get("/public/quizzes")

    assert response.status_code == 200
    assert "Course quiz library" in response.text
    assert "Neuro" in response.text
    assert "Exam 1" in response.text
    assert "Lecture 1" in response.text
    assert published.token in response.text
    assert "Unpublished lecture" not in response.text


def test_practice_questions_are_not_listed_as_lecture_quizzes(tmp_path):
    app, lecture_quiz, practice = _published_mixed_app(tmp_path)
    client = TestClient(app)

    quizzes = client.get("/public/quizzes")
    practice_page = client.get("/public/practice-questions")

    assert quizzes.status_code == 200
    assert lecture_quiz.token in quizzes.text
    assert practice.token not in quizzes.text
    assert practice_page.status_code == 200
    assert practice.token in practice_page.text
    assert lecture_quiz.token not in practice_page.text
    assert 'href="/public/practice-questions"' in quizzes.text
    assert 'href="/public/quizzes"' in practice_page.text


def test_practice_player_returns_to_the_practice_question_library(tmp_path):
    app, _, practice = _published_mixed_app(tmp_path)

    response = TestClient(app).get(f"/public/quizzes/{practice.token}")

    assert response.status_code == 200
    assert 'href="/public/practice-questions"' in response.text
    assert 'aria-label="Back to practice questions"' in response.text


def test_public_library_root_uses_same_access_boundary_as_quiz_pages(tmp_path):
    app, _ = _published_app(tmp_path, public=True)

    response = TestClient(
        app,
        base_url="https://study.example.com",
    ).get("/public/quizzes", headers={"host": "study.example.com"})

    assert response.status_code == 200


def test_practice_library_is_public_while_private_routes_remain_blocked(tmp_path):
    app, _, practice = _published_mixed_app(tmp_path, public=True)
    headers = {"host": "study.example.com"}

    with TestClient(app, base_url="https://study.example.com") as client:
        library = client.get("/public/practice-questions", headers=headers)
        private_dashboard = client.get("/", headers=headers)

    assert library.status_code == 200
    assert practice.token in library.text
    assert private_dashboard.status_code == 503


def test_public_outline_uses_quiz_token_and_returns_current_pdf(tmp_path):
    app, published = _published_app(tmp_path)
    path = tmp_path / "study" / "Neuro" / "outline.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = OutlinePdfRenderer().render("Neuro Outline", "# Topic\nContent")
    path.write_bytes(payload)
    job = app.state.generation_repository.queue(
        published.lecture_id,
        GenerationKind.OUTLINE,
    )
    app.state.generation_repository.record_outline(
        published.lecture_id,
        job.id,
        path,
        hashlib.sha256(payload).hexdigest(),
    )

    client = TestClient(app)
    library = client.get("/public/quizzes")
    response = client.get(
        f"/public/quizzes/{published.token}/outline",
    )

    assert "Lecture Outline" in library.text
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_public_outline_is_not_available_without_current_outline(tmp_path):
    app, published = _published_app(tmp_path)

    response = TestClient(app).get(
        f"/public/quizzes/{published.token}/outline",
    )

    assert response.status_code == 404


def test_public_quiz_page_and_content_do_not_expose_answer_key(tmp_path):
    app, published = _published_app(tmp_path)

    with TestClient(app) as client:
        page = client.get(f"/public/quizzes/{published.token}")
        content = client.get(f"/public/quizzes/{published.token}/content")

    assert page.status_code == 200
    assert "Lecture 1 Practice Quiz" in page.text
    assert 'class="quiz-library-button"' in page.text
    assert "/public/quizzes/assets/player.css?v=" in page.text
    assert "/public/quizzes/assets/player.js?v=" in page.text
    assert page.headers["content-security-policy"].startswith(
        "default-src 'self'"
    )
    assert content.status_code == 200
    assert content.json()["version"] == 1
    assert content.json()["course"] == "Neuro"
    assert content.json()["questions"][0]["choices"][0] == {
        "id": "c1",
        "text": "Lysis of erythroid precursors",
    }
    assert "correct_index" not in content.text
    assert "correct_choice_id" not in content.text
    assert "rationale" not in content.text


def test_public_quiz_assets_are_served_inside_the_bypass_path(tmp_path):
    app, _ = _published_app(tmp_path)

    with TestClient(app) as client:
        script = client.get("/public/quizzes/assets/player.js")
        styles = client.get("/public/quizzes/assets/player.css")
        library_script = client.get("/public/quizzes/assets/library.js")
        library_styles = client.get("/public/quizzes/assets/library.css")
        tokens = client.get("/public/quizzes/assets/tokens.css")

    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")
    assert library_script.status_code == 200
    assert library_styles.status_code == 200
    assert tokens.status_code == 200
    assert tokens.headers["content-type"].startswith("text/css")


def test_public_quiz_player_markup_uses_content_versioned_assets(tmp_path):
    app, published = _published_app(tmp_path)

    with TestClient(app) as client:
        first = client.get(f"/public/quizzes/{published.token}")
        second = client.get(f"/public/quizzes/{published.token}")

    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert "?v=" in first.text
    assert first.text == second.text


def test_answer_feedback_is_limited_to_the_requested_question(tmp_path):
    app, published = _published_app(tmp_path)

    with TestClient(app) as client:
        page = client.get(f"/public/quizzes/{published.token}")
        csrf = page.cookies.get("study_hub_csrf")
        response = client.post(
            f"/public/quizzes/{published.token}/answer",
            json={"question_id": "q1", "choice_id": "c2"},
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 200
    assert response.json() == {
        "correct": False,
        "correct_choice_id": "c1",
        "rationale": "Parvovirus B19 infects erythroid precursor cells.",
    }


def test_unknown_public_quiz_token_returns_not_found(tmp_path):
    app, _ = _published_app(tmp_path)

    with TestClient(app) as client:
        response = client.get(f"/public/quizzes/{'f' * 64}")

    assert response.status_code == 404


def test_only_public_quiz_paths_bypass_cloudflare_access(tmp_path):
    app, published = _published_app(tmp_path, public=True)
    headers = {"host": "study.example.com"}

    with TestClient(app, base_url="https://study.example.com") as client:
        quiz = client.get(
            f"/public/quizzes/{published.token}",
            headers=headers,
        )
        dashboard = client.get("/", headers=headers)

    assert quiz.status_code == 200
    assert dashboard.status_code == 503
    assert dashboard.json()["detail"] == "Cloudflare Access is not configured"


def test_public_answer_submission_still_requires_csrf(tmp_path):
    app, published = _published_app(tmp_path, public=True)
    path = f"/public/quizzes/{published.token}"

    with TestClient(app, base_url="https://study.example.com") as client:
        rejected = client.post(
            f"{path}/answer",
            json={"question_id": "q1", "choice_id": "c1"},
        )
        page = client.get(path)
        csrf = page.cookies.get("study_hub_csrf")
        accepted = client.post(
            f"{path}/answer",
            json={"question_id": "q1", "choice_id": "c1"},
            headers={
                "origin": "https://study.example.com",
                "X-CSRF-Token": csrf,
            },
        )

    assert rejected.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["correct"] is True


def test_public_library_and_content_include_studio_quizzes(tmp_path):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}",
    )
    app = create_app(settings)
    run = app.state.studio_repository.queue_run(
        "Professor Review",
        2,
        "Create a quiz.",
        [],
        "Professor Review Quiz",
        "Professor Review",
        2,
    )
    published = app.state.generation_repository.publish_studio_quiz(run.id, _quiz())

    client = TestClient(app)
    library = client.get("/public/quizzes")
    content = client.get(f"/public/quizzes/{published.token}/content")
    page = client.get(f"/public/quizzes/{published.token}")

    assert library.status_code == 200
    assert "Studio quiz" in library.text
    assert "Professor Review Quiz" in library.text
    assert content.status_code == 200
    assert content.json()["course"] == "Professor Review"
    assert content.json()["exam_number"] == 2
    assert "lecture_number" not in content.json()
    assert page.status_code == 200
    assert "Exam 2" in page.text
