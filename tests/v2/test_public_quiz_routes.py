import hashlib
import json

from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.models import StudioRunModel
from oms_hub.repositories import LectureInput
from oms_hub.security.rate_limit import PublicQuizRateLimiter, RatePolicy
from oms_hub.study_generation.domain import GenerationKind, PublishedQuizOrderDirection
from oms_hub.study_generation.native_quiz import parse_native_quiz
from oms_hub.study_generation.outline import OutlinePdfRenderer
from oms_hub.web import public_quiz_routes


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
    assert "Neuro Lecture 01" in response.text
    assert published.token in response.text
    assert "Unpublished lecture" not in response.text


def test_public_library_starts_all_courses_and_exams_collapsed(tmp_path):
    app, _ = _published_app(tmp_path)
    cardio_lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Cardio", 1, 1, "Arrhythmias", "", None)
    )
    cardio_job = app.state.generation_repository.queue(
        cardio_lecture_id,
        GenerationKind.QUIZ,
    )
    app.state.generation_repository.publish_quiz(
        cardio_lecture_id,
        cardio_job.id,
        _quiz("Cardio quiz"),
    )

    response = TestClient(app).get("/public/quizzes")

    assert response.status_code == 200
    assert response.text.count('class="course-card sh-card"') == 2
    assert 'aria-expanded="true"' not in response.text
    assert response.text.count('class="course-content" hidden') == 2
    assert response.text.count('class="lecture-list" hidden') == 2


def test_management_library_defers_structured_editor_payload_to_owner(tmp_path):
    app, published = _published_app(tmp_path)
    public = TestClient(app).get("/public/quizzes")
    managed = TestClient(app).get("/studio/library/quizzes")

    assert "data-payload-questions" not in public.text
    assert "correct_index" not in public.text
    assert "data-payload-questions" in managed.text
    assert f'data-payload-url="/api/published-quizzes/{published.token}/payload"' in managed.text
    assert "<fieldset data-payload-question" not in managed.text
    assert "data-add-question" in managed.text
    assert "Open to load this quiz’s questions." in managed.text
    assert "/static/public_quiz_library.js?v=" in managed.text
    assert 'src="/public/quizzes/assets/' not in managed.text


def test_management_payload_endpoint_returns_answers_only_to_owner(tmp_path):
    app, published = _published_app(tmp_path)

    response = TestClient(app).get(f"/api/published-quizzes/{published.token}/payload")

    assert response.status_code == 200
    assert response.json()["title"] == published.title
    assert response.json()["questions"][0]["correct_index"] == 0
    assert response.json()["questions"][0]["rationale"] == published.quiz.questions[0].rationale


def test_public_question_flags_require_csrf_group_and_notify_management(tmp_path):
    app, published = _published_app(tmp_path)
    url = f"/public/quizzes/{published.token}/flags"
    payload = {
        "version": published.version,
        "question_id": "q1",
        "reason": "inaccurate_question",
    }

    with TestClient(app) as client:
        denied = client.post(url, json=payload)
        client.get(f"/public/quizzes/{published.token}")
        csrf = client.cookies.get("study_hub_csrf")
        first = client.post(url, json=payload, headers={"X-CSRF-Token": csrf})
        second = client.post(url, json=payload, headers={"X-CSRF-Token": csrf})
        invalid_reason = client.post(
            url,
            json={**payload, "reason": "arbitrary"},
            headers={"X-CSRF-Token": csrf},
        )
        stale = client.post(
            url,
            json={**payload, "version": published.version + 1},
            headers={"X-CSRF-Token": csrf},
        )
        wrong_question = client.post(
            url,
            json={**payload, "question_id": "q9"},
            headers={"X-CSRF-Token": csrf},
        )
        management = client.get("/studio/library/quizzes")
        flags = client.get(f"/api/published-quizzes/{published.token}/flags")

    assert denied.status_code == 403
    assert first.status_code == second.status_code == 200
    assert invalid_reason.status_code == 422
    assert stale.status_code == wrong_question.status_code == 409
    assert flags.json()["flags"] == [
        {
            "question_id": "q1",
            "reason": "inaccurate_question",
            "count": 2,
            "version": published.version,
        }
    ]
    assert 'aria-label="1 open question flag"' in management.text


def test_public_hostname_allows_rate_limited_flags_but_not_management(tmp_path):
    app, published = _published_app(tmp_path, public=True)
    app.state.public_quiz_rate_limiter = PublicQuizRateLimiter(
        general_client=RatePolicy(2, 60),
        general_global=RatePolicy(10, 60),
        outline_client=RatePolicy(2, 60),
        clock=lambda: 100.0,
    )
    payload = {
        "version": published.version,
        "question_id": "q1",
        "reason": "want_to_review",
    }

    with TestClient(app, base_url="https://study.example.com") as client:
        client.get(f"/public/quizzes/{published.token}")
        csrf = client.cookies.get("study_hub_csrf")
        recorded = client.post(
            f"/public/quizzes/{published.token}/flags",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        limited = client.post(
            f"/public/quizzes/{published.token}/flags",
            json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        management = client.get("/studio/library/quizzes")

    assert recorded.status_code == 200
    assert limited.status_code == 429
    assert management.status_code == 503


def test_structured_payload_edit_versions_quiz_resolves_flags_and_rejects_unknown_media(
    tmp_path,
):
    app, published = _published_app(tmp_path)
    flag_payload = {
        "version": published.version,
        "question_id": "q1",
        "reason": "ambiguous_question",
    }
    edited = {
        "title": published.title,
        "questions": [
            {
                "stem": "Corrected stem?",
                "choices": ["First", "Second", "Third"],
                "correct_index": 1,
                "rationale": "Second is correct.",
                "image_ref": None,
            },
            {
                "stem": "Added question?",
                "choices": ["Yes", "No"],
                "correct_index": 0,
                "rationale": "Yes.",
                "image_ref": None,
            },
        ],
    }
    invented_media = {
        **edited,
        "questions": [
            {
                **edited["questions"][0],
                "image_ref": {
                    "key": "invented-image",
                    "source_title": "Slides",
                    "locator": "slide 1",
                    "description": "diagram",
                },
            }
        ],
    }

    with TestClient(app) as client:
        client.get(f"/public/quizzes/{published.token}")
        csrf = client.cookies.get("study_hub_csrf")
        client.post(
            f"/public/quizzes/{published.token}/flags",
            json=flag_payload,
            headers={"X-CSRF-Token": csrf},
        )
        rejected = client.patch(
            f"/api/published-quizzes/{published.token}/payload",
            json={"payload_json": json.dumps(invented_media)},
            headers={"X-CSRF-Token": csrf},
        )
        updated = client.patch(
            f"/api/published-quizzes/{published.token}/payload",
            json={"payload_json": json.dumps(edited)},
            headers={"X-CSRF-Token": csrf},
        )
        flags = client.get(f"/api/published-quizzes/{published.token}/flags")
        content = client.get(f"/public/quizzes/{published.token}/content")

    assert rejected.status_code == 422
    assert "unavailable image media" in rejected.json()["detail"]
    assert updated.status_code == 200
    assert updated.json()["version"] == published.version + 1
    assert flags.json() == {"flags": []}
    assert content.json()["version"] == published.version + 1
    assert [question["stem"] for question in content.json()["questions"]] == [
        "Corrected stem?",
        "Added question?",
    ]
    assert "correct_index" not in content.text


def test_question_payload_edit_keeps_an_authoritatively_renamed_title(tmp_path):
    app, published = _published_app(tmp_path)
    with TestClient(app) as client:
        client.get(f"/public/quizzes/{published.token}")
        csrf = client.cookies.get("study_hub_csrf")
        renamed = client.patch(
            f"/api/published-quizzes/{published.token}/title",
            json={"title": "Renamed quiz"}, headers={"X-CSRF-Token": csrf},
        )
        updated = client.patch(
            f"/api/published-quizzes/{published.token}/payload",
            json={"payload_json": json.dumps({
                "title": published.title,
                "questions": [{"stem": "Updated?", "choices": ["A", "B"],
                               "correct_index": 0, "rationale": "A."}],
            })}, headers={"X-CSRF-Token": csrf},
        )
        content = client.get(f"/public/quizzes/{published.token}/content")
    assert renamed.status_code == updated.status_code == 200
    assert content.json()["title"] == "Renamed quiz"


def test_local_owner_library_keeps_private_navigation_without_management_controls(tmp_path):
    app, published = _published_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        public = client.get("/public/quizzes")
        managed = client.get("/studio/library/quizzes")

    assert public.status_code == 200
    assert managed.status_code == 200
    assert published.token in public.text
    assert "Study Hub Quizzes" not in public.text
    assert 'href="/">Home</a>' in public.text
    assert 'href="/lectures">Lectures</a>' in public.text
    assert "NUC online" not in public.text
    assert 'href="/">Home</a>' in managed.text
    assert 'href="/lectures">Lectures</a>' in managed.text
    assert "Study Hub Quizzes" not in managed.text
    assert 'data-reset-quiz' in public.text
    assert f'title="Restart {published.title}"' in public.text
    for private_hook in (
        "data-quiz-drag-handle",
        "data-title-form",
        "data-move-quiz-library",
        "data-remove-quiz",
    ):
        assert private_hook not in public.text
        assert private_hook in managed.text
    assert "Quiz Builder management" in managed.text
    assert "/studio/library/practice-questions" in managed.text
    assert "Reset quiz progress" not in public.text


def test_public_host_library_hides_private_owner_navigation(tmp_path):
    app, published = _published_app(tmp_path, public=True)

    response = TestClient(app, base_url="https://study.example.com").get(
        "/public/quizzes",
        headers={"host": "study.example.com"},
    )

    assert response.status_code == 200
    assert published.token in response.text
    assert "Study Hub Quizzes" in response.text
    assert 'href="/">Dashboard</a>' not in response.text
    assert 'href="/anki">Anki</a>' not in response.text
    assert 'href="/settings">Settings</a>' not in response.text


def test_public_library_uses_current_lecture_scope_and_repository_order(tmp_path):
    app, published = _published_app(tmp_path)
    app.state.catalog_repository.update_lecture(
        published.lecture_id,
        LectureInput("Cardio", 2, 1, "Arrhythmias", "", None),
    )
    peer_lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Cardio", 2, 2, "Heart failure", "", None)
    )
    peer = app.state.generation_repository.publish_quiz(
        peer_lecture_id,
        app.state.generation_repository.queue(peer_lecture_id, GenerationKind.QUIZ).id,
        _quiz("Peer quiz"),
    )
    app.state.generation_repository.reorder_published_quiz(
        published.token,
        PublishedQuizOrderDirection.DOWN,
    )

    library = TestClient(app).get("/public/quizzes")

    assert "Cardio" in library.text
    assert "Exam 2" in library.text
    assert published.token in library.text
    assert peer.token in library.text
    assert library.text.index(peer.token) < library.text.index(published.token)


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
    assert "General CNS Pathology" in page.text
    assert 'class="quiz-library-button sh-btn sh-btn--secondary"' in page.text
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
    library_version = public_quiz_routes._library_asset_version()

    with TestClient(app) as client:
        script = client.get("/public/quizzes/assets/player.js")
        styles = client.get("/public/quizzes/assets/player.css")
        library_script = client.get("/public/quizzes/assets/library.js")
        versioned_library_script = client.get(
            f"/public/quizzes/assets/{library_version}/library.js"
        )
        library_styles = client.get("/public/quizzes/assets/library.css")
        reset = client.get("/public/quizzes/assets/reset.css")
        tokens = client.get("/public/quizzes/assets/tokens.css")
        shared = client.get("/public/quizzes/assets/study-hub.css")
        head_responses = [
            client.head(f"/public/quizzes/assets/{name}")
            for name in (
                "player.js",
                "player.css",
                "library.js",
                "library.css",
                "reset.css",
                "tokens.css",
                "study-hub.css",
            )
        ]

    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert styles.status_code == 200
    assert styles.headers["content-type"].startswith("text/css")
    assert library_script.status_code == 200
    assert versioned_library_script.status_code == 200
    assert versioned_library_script.content == library_script.content
    assert library_styles.status_code == 200
    assert reset.status_code == 200
    assert reset.headers["content-type"].startswith("text/css")
    assert tokens.status_code == 200
    assert tokens.headers["content-type"].startswith("text/css")
    assert shared.status_code == 200
    assert shared.headers["content-type"].startswith("text/css")
    assert all(response.status_code == 200 for response in head_responses)


def test_public_quiz_player_markup_uses_content_versioned_assets(tmp_path):
    app, published = _published_app(tmp_path)

    with TestClient(app) as client:
        first = client.get(f"/public/quizzes/{published.token}")
        second = client.get(f"/public/quizzes/{published.token}")

    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert "?v=" in first.text
    assert "/public/quizzes/assets/reset.css?v=" in first.text
    assert "/public/quizzes/assets/tokens.css?v=" in first.text
    assert "/public/quizzes/assets/study-hub.css?v=" in first.text
    assert "/static/reset.css" not in first.text
    assert "/static/tokens.css" not in first.text
    assert "/static/study-hub.css" not in first.text
    assert first.text == second.text


def test_public_quiz_library_markup_uses_content_versioned_assets(
    tmp_path,
    monkeypatch,
):
    app, _, _ = _published_mixed_app(tmp_path)
    current_version = public_quiz_routes._library_asset_version()

    with TestClient(app) as client:
        quizzes = client.get("/public/quizzes")
        practice = client.get("/public/practice-questions")

        assert f"/library.css?v={current_version}" in quizzes.text
        assert f"/assets/{current_version}/library.js" in quizzes.text
        assert f"/library.css?v={current_version}" in practice.text
        assert f"/assets/{current_version}/library.js" in practice.text
        for page in (quizzes.text, practice.text):
            assert f"/reset.css?v={current_version}" in page
            assert f"/tokens.css?v={current_version}" in page
            assert f"/study-hub.css?v={current_version}" in page
            assert "/static/study-hub.css" not in page

        def changed_digest(path):
            return "a" * 64 if path.suffix == ".js" else "b" * 64

        monkeypatch.setattr(public_quiz_routes, "sha256_file", changed_digest)
        changed_version = public_quiz_routes._library_asset_version()
        changed = client.get("/public/quizzes")

    assert changed_version != current_version
    assert f"/library.css?v={changed_version}" in changed.text
    assert f"/assets/{changed_version}/library.js" in changed.text


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


def test_published_quiz_management_unpublishes_lecture_and_studio_tokens(tmp_path):
    app, lecture, studio = _published_mixed_app(tmp_path)

    with TestClient(app) as client:
        client.get("/public/quizzes")
        csrf = client.cookies.get("study_hub_csrf")
        lecture_response = client.delete(
            f"/api/published-quizzes/{lecture.token}",
            headers={"X-CSRF-Token": csrf},
        )
        studio_response = client.delete(
            f"/api/published-quizzes/{studio.token}",
            headers={"X-CSRF-Token": csrf},
        )
        already_inactive = client.delete(
            f"/api/published-quizzes/{studio.token}",
            headers={"X-CSRF-Token": csrf},
        )

    assert lecture_response.status_code == 200
    assert lecture_response.json() == {
        "token": lecture.token,
        "state": "unpublished",
        "course_key": "neuro",
        "exam_number": 1,
        "exam_key": "neuro:1",
        "course_quiz_count": 0,
        "exam_quiz_count": 0,
    }
    assert studio_response.status_code == 200
    assert studio_response.json() == {
        "token": studio.token,
        "state": "unpublished",
        "course_key": "neuro",
        "exam_number": 1,
        "exam_key": "neuro:1",
        "course_quiz_count": 0,
        "exam_quiz_count": 0,
    }
    assert already_inactive.status_code == 404
    assert app.state.generation_repository.published_quiz(lecture.token) is None
    assert app.state.generation_repository.published_quiz(studio.token) is None


def test_unpublish_returns_authoritative_remaining_library_counts(tmp_path):
    app, published = _published_app(tmp_path)
    peer_lecture = app.state.catalog_repository.upsert_lecture(
        LectureInput("Neuro", 1, 2, "Peer", "", None)
    )
    peer_job = app.state.generation_repository.queue(peer_lecture, GenerationKind.QUIZ)
    app.state.generation_repository.publish_quiz(peer_lecture, peer_job.id, _quiz("Peer"))

    with TestClient(app) as client:
        client.get("/public/quizzes")
        response = client.delete(
            f"/api/published-quizzes/{published.token}",
            headers={"X-CSRF-Token": client.cookies.get("study_hub_csrf")},
        )

    assert response.status_code == 200
    assert response.json()["course_quiz_count"] == 1
    assert response.json()["exam_quiz_count"] == 1


def test_published_quiz_management_requires_csrf_and_active_token(tmp_path):
    app, published = _published_app(tmp_path)

    with TestClient(app) as client:
        missing_csrf = client.delete(f"/api/published-quizzes/{published.token}")
        client.get("/public/quizzes")
        csrf = client.cookies.get("study_hub_csrf")
        unknown = client.delete(
            f"/api/published-quizzes/{'f' * 64}",
            headers={"X-CSRF-Token": csrf},
        )

    assert missing_csrf.status_code == 403
    assert unknown.status_code == 404


def test_published_quiz_management_edits_title_and_moves_library_section(tmp_path):
    app, published = _published_app(tmp_path)

    with TestClient(app) as client:
        client.get("/public/quizzes")
        csrf = client.cookies.get("study_hub_csrf")
        renamed = client.patch(
            f"/api/published-quizzes/{published.token}/title",
            json={"title": "  Revised quiz title  "},
            headers={"X-CSRF-Token": csrf},
        )
        moved = client.patch(
            f"/api/published-quizzes/{published.token}/library",
            json={"section": "practice_questions"},
            headers={"X-CSRF-Token": csrf},
        )
        content = client.get(f"/public/quizzes/{published.token}/content")
        quizzes = client.get("/public/quizzes")
        practice = client.get("/public/practice-questions")

    assert renamed.json() == {"token": published.token, "title": "Revised quiz title"}
    assert moved.json()["content_kind"] == "practice_questions"
    assert content.json()["title"] == "Revised quiz title"
    assert published.token not in quizzes.text
    assert published.token in practice.text


def test_published_quiz_patch_management_validates_csrf_payload_and_token(tmp_path):
    app, published = _published_app(tmp_path)
    title_path = f"/api/published-quizzes/{published.token}/title"
    library_path = f"/api/published-quizzes/{published.token}/library"
    order_path = f"/api/published-quizzes/{published.token}/order"

    with TestClient(app) as client:
        csrf_rejections = [
            client.patch(title_path, json={"title": "Edited"}),
            client.patch(library_path, json={"section": "practice_questions"}),
            client.patch(order_path, json={"direction": "up"}),
        ]
        client.get("/public/quizzes")
        csrf = client.cookies.get("study_hub_csrf")
        blank_title = client.patch(
            title_path,
            json={"title": "   "},
            headers={"X-CSRF-Token": csrf},
        )
        long_title = client.patch(
            title_path,
            json={"title": "x" * 301},
            headers={"X-CSRF-Token": csrf},
        )
        invalid_section = client.patch(
            library_path,
            json={"section": "other"},
            headers={"X-CSRF-Token": csrf},
        )
        invalid_direction = client.patch(
            order_path,
            json={"direction": "sideways"},
            headers={"X-CSRF-Token": csrf},
        )
        unknown = client.patch(
            f"/api/published-quizzes/{'f' * 64}/title",
            json={"title": "Edited"},
            headers={"X-CSRF-Token": csrf},
        )
        app.state.generation_repository.unpublish_quiz(published.token)
        inactive = client.patch(
            order_path,
            json={"direction": "up"},
            headers={"X-CSRF-Token": csrf},
        )

    assert all(response.status_code == 403 for response in csrf_rejections)
    assert blank_title.status_code == 422
    assert long_title.status_code == 422
    assert invalid_section.status_code == 422
    assert invalid_direction.status_code == 422
    assert unknown.status_code == 404
    assert inactive.status_code == 404


def test_published_quiz_patch_management_is_not_in_public_access_bypass(tmp_path):
    app, published = _published_app(tmp_path, public=True)
    headers = {"host": "study.example.com"}

    with TestClient(app, base_url="https://study.example.com") as client:
        client.get("/public/quizzes", headers=headers)
        csrf = client.cookies.get("study_hub_csrf")
        responses = [
            client.patch(
                f"/api/published-quizzes/{published.token}/title",
                json={"title": "Edited"},
                headers={**headers, "X-CSRF-Token": csrf},
            ),
            client.patch(
                f"/api/published-quizzes/{published.token}/library",
                json={"section": "practice_questions"},
                headers={**headers, "X-CSRF-Token": csrf},
            ),
            client.patch(
                f"/api/published-quizzes/{published.token}/order",
                json={"direction": "up"},
                headers={**headers, "X-CSRF-Token": csrf},
            ),
        ]

    assert all(response.status_code == 503 for response in responses)


def test_published_quiz_management_is_not_in_public_access_bypass(tmp_path):
    app, published = _published_app(tmp_path, public=True)
    headers = {"host": "study.example.com"}

    with TestClient(app, base_url="https://study.example.com") as client:
        page = client.get("/public/quizzes", headers=headers)
        csrf = client.cookies.get("study_hub_csrf")
        response = client.delete(
            f"/api/published-quizzes/{published.token}",
            headers={**headers, "X-CSRF-Token": csrf},
        )

    assert page.status_code == 200
    assert csrf is not None
    assert response.status_code == 503
    assert app.state.generation_repository.published_quiz(published.token) is not None


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
    assert "Lecture 1 Practice Quiz" in library.text
    assert "Studio quiz" not in library.text
    assert content.status_code == 200
    assert content.json()["course"] == "Professor Review"
    assert content.json()["exam_number"] == 2
    assert "lecture_number" not in content.json()
    assert page.status_code == 200
    assert "Exam 2" in page.text


def test_mixed_library_uses_studio_label_and_lecture_number(tmp_path):
    app, lecture, practice = _published_mixed_app(tmp_path)

    library = TestClient(app).get("/public/practice-questions")
    quiz_library = TestClient(app).get("/public/quizzes")

    assert practice.token in library.text
    assert "Practice Questions" in library.text
    assert "Studio quiz" not in library.text
    assert lecture.token in quiz_library.text
    assert "Neuro Lecture 01" in quiz_library.text
    assert "General CNS Pathology" in quiz_library.text
