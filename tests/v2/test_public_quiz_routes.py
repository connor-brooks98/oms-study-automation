import json

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.repositories import LectureInput
from oms_hub.study_generation.domain import GenerationKind
from oms_hub.study_generation.native_quiz import parse_native_quiz


def _quiz():
    return parse_native_quiz(
        json.dumps(
            {
                "title": "Lecture 1 Practice Quiz",
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


def test_public_quiz_page_and_content_do_not_expose_answer_key(tmp_path):
    app, published = _published_app(tmp_path)

    with TestClient(app) as client:
        page = client.get(f"/public/quizzes/{published.token}")
        content = client.get(f"/public/quizzes/{published.token}/content")

    assert page.status_code == 200
    assert "Lecture 1 Practice Quiz" in page.text
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
