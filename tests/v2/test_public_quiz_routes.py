import hashlib
import json
import os
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.repositories import LectureInput
from oms_hub.security.rate_limit import PublicQuizRateLimiter, RatePolicy
from oms_hub.study_generation.domain import GenerationKind
from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.outline import OutlinePdfRenderer
from oms_hub.study_generation.studio_domain import StudioStoredImage
from oms_hub.web import artifact_routes
from tests.support import csrf_client


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
                        "rationale": ("Parvovirus B19 infects erythroid precursor cells."),
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


def _published_studio_image_app(tmp_path):
    app, _lecture_quiz = _published_app(tmp_path)
    run = app.state.studio_repository.queue_run(
        "Neuro",
        1,
        "Prompt",
        [],
        "Shared image review",
        "Neuro",
        1,
    )
    image_ref = {
        "key": "image-1",
        "source_title": "Slides",
        "locator": "Slide 4",
        "description": "Shared pathology image",
    }
    quiz = parse_native_quiz(
        json.dumps(
            {
                "title": "Shared image review",
                "questions": [
                    {
                        "stem": f"Question {number}",
                        "choices": ["A", "B"],
                        "correct_index": 0,
                        "rationale": "A is correct.",
                        "image_ref": image_ref,
                    }
                    for number in (1, 2)
                ],
            }
        )
    )
    app.state.studio_repository.await_image_review(
        run.id,
        "notebook-1",
        "raw",
        quiz,
    )
    output = BytesIO()
    Image.new("RGB", (10, 6), "green").save(output, format="PNG")
    payload = output.getvalue()
    path = tmp_path / "media" / "image.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    app.state.studio_repository.bind_image(
        run.id,
        "image-1",
        StudioStoredImage(
            path,
            hashlib.sha256(payload).hexdigest(),
            "image/png",
            10,
            6,
            "image.png",
        ),
    )
    published = app.state.generation_repository.publish_reviewed_studio_quiz(run.id)
    return app, run, published


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


def test_public_library_root_uses_same_access_boundary_as_quiz_pages(tmp_path):
    app, _ = _published_app(tmp_path, public=True)

    response = TestClient(
        app,
        base_url="https://study.example.com",
    ).get("/public/quizzes", headers={"host": "study.example.com"})

    assert response.status_code == 200


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
    assert (
        'src="/public/quizzes/assets/player.js?v=20260801-image-support"'
        in page.text
    )
    assert (
        'href="/public/quizzes/assets/player.css?v=20260801-image-support"'
        in page.text
    )
    assert page.headers["content-security-policy"].startswith("default-src 'self'")
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


def test_public_image_quiz_reuses_bound_media_without_private_metadata(tmp_path):
    app, _run, published = _published_studio_image_app(tmp_path)
    client = TestClient(app)

    content = client.get(f"/public/quizzes/{published.token}/content")
    media = client.get(f"/public/quizzes/{published.token}/media/image-1")
    missing = client.get(f"/public/quizzes/{published.token}/media/image-9")

    expected_url = f"/public/quizzes/{published.token}/media/image-1"
    assert [question["image_url"] for question in content.json()["questions"]] == [
        expected_url,
        expected_url,
    ]
    assert [question["image_alt"] for question in content.json()["questions"]] == [
        "Shared pathology image",
        "Shared pathology image",
    ]
    assert "locator" not in content.text
    assert "source_title" not in content.text
    assert media.status_code == 200
    assert media.headers["content-type"] == "image/png"
    assert media.headers["cache-control"] == "no-store"
    assert missing.status_code == 404


def test_unpublishing_image_quiz_removes_public_media_access(tmp_path):
    app, run, published = _published_studio_image_app(tmp_path)
    client = csrf_client(app)
    path = f"/public/quizzes/{published.token}/media/image-1"
    assert client.get(path).status_code == 200

    assert client.delete(f"/studio/runs/{run.id}/publication").status_code == 200

    assert client.get(path).status_code == 404


def test_tampered_published_image_is_not_served(tmp_path):
    app, _run, published = _published_studio_image_app(tmp_path)
    media = app.state.generation_repository.published_quiz_media(published.token)[0]
    media.path.write_bytes(b"tampered")

    response = TestClient(app).get(
        f"/public/quizzes/{published.token}/media/{media.image_key}"
    )

    assert response.status_code == 404


def test_studio_quiz_is_grouped_by_destination_without_lecture_metadata(tmp_path):
    app, _lecture_quiz = _published_app(tmp_path)
    run = app.state.studio_repository.queue_run(
        "Neuro",
        1,
        "Prompt with contract",
        [],
        "Course Review",
        "Cardiology",
        3,
    )
    quiz = _quiz()
    from dataclasses import replace

    published = app.state.generation_repository.publish_studio_quiz(
        run.id,
        replace(quiz, title=run.label),
    )

    client = TestClient(app)
    library = client.get("/public/quizzes")
    page = client.get(f"/public/quizzes/{published.token}")
    content = client.get(f"/public/quizzes/{published.token}/content")

    assert "Cardiology" in library.text
    assert "Exam 3" in library.text
    assert "Course Review" in library.text
    assert "Cardiology · Exam 3" in page.text
    assert "Lecture" not in page.text
    assert content.json()["course"] == "Cardiology"
    assert "lecture_number" not in content.json()
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
    assert script.headers["cache-control"] == "no-cache"
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")
    assert styles.headers["cache-control"] == "no-cache"
    assert library_script.status_code == 200
    assert library_styles.status_code == 200
    assert tokens.status_code == 200
    assert "--brand:" in tokens.text


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


def test_public_quiz_limit_returns_friendly_retry_response(tmp_path):
    app, published = _published_app(tmp_path)
    app.state.public_quiz_rate_limiter = PublicQuizRateLimiter(
        general_client=RatePolicy(1, 0),
        general_global=RatePolicy(10, 0),
    )
    path = f"/public/quizzes/{published.token}"

    with TestClient(app) as client:
        assert client.get(path).status_code == 200
        limited = client.get(path)

    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert limited.json()["detail"] == (
        "Too many quiz requests. Please wait a moment and try again."
    )


def test_unchanged_outline_validation_is_cached_by_file_metadata(
    tmp_path,
    monkeypatch,
):
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
    calls = 0
    original = artifact_routes.sha256_file

    def counted(candidate):
        nonlocal calls
        calls += 1
        return original(candidate)

    artifact_routes._validate_outline_pdf.cache_clear()
    monkeypatch.setattr(artifact_routes, "sha256_file", counted)
    url = f"/public/quizzes/{published.token}/outline"
    with TestClient(app) as client:
        assert client.get(url).status_code == 200
        assert client.get(url).status_code == 200
        metadata = path.stat()
        os.utime(
            path,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
        )
        assert client.get(url).status_code == 200

    assert calls == 2
