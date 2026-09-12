from fastapi import Request
from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings


def test_private_identity_is_not_assigned_to_public_paths(tmp_path):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}", study_root=tmp_path / "study"))

    @app.get("/study/owner-probe")
    def probe(request: Request):
        return {"owner": getattr(request.state, "study_owner_id", None)}

    @app.middleware("http")
    async def expose_test_identity(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Test-Owner"] = getattr(request.state, "study_owner_id", "none")
        return response

    with TestClient(app) as client:
        assert client.get("/study/owner-probe").json() == {"owner": "local-owner"}
        public = client.get("/public/quizzes/owner-probe", headers={"X-Owner": "local-owner"})
        assert public.headers["X-Test-Owner"] == "none"


def test_chat_app_wiring_shares_client_and_enforces_source_owner(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    import oms_hub.app as app_module
    from oms_hub.study_chat.contracts import ChatRequest

    fake = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(app_module, "CodexSessionClient", lambda *a, **k: fake)
    app = create_app(Settings(_env_file=None, data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}", study_root=tmp_path / "study",
        codex_executable=tmp_path / "fixture", codex_binary_sha256="a" * 64,
        codex_model="fixture"))
    assert app.state.study_chat_service.client is app.state.codex_session is fake
    assert app.state.gpt_transcript_cleaner.client is fake
    assert app.state.study_topic_service.client is fake
    app.state.codex_model = "new-fixture"
    assert app.state.study_topic_service.model() == "new-fixture"
    repository = app.state.study_chat_repository
    with pytest.raises(PermissionError):
        repository.sources.snapshot("other", (1,))
    conversation = repository.create("local-owner", "general", ())
    from uuid import uuid4
    identity = str(uuid4())
    repository.begin(ChatRequest(identity, "local-owner", conversation, "general", "iron", ()),
                     model="fixture")
    # App startup retains unfinished requests as interrupted, without submitting a turn.
    with TestClient(app) as client:
        response = client.get("/study/chat")
        assert response.status_code == 200 and 'href="/study/chat"' in response.text
        assert repository.load_request(identity, owner_id="local-owner").state == "interrupted"
    app.state.database.close()


def test_personal_session_app_wiring_and_public_isolation(tmp_path):
    import pytest

    from oms_hub.models import PublishedQuizMediaModel, PublishedQuizModel
    from oms_hub.question_bank.contracts import QuestionKey

    app = create_app(Settings(_env_file=None, data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}", study_root=tmp_path / "study"))
    token = "a" * 64
    payload = ('{"title":"Local fixture","questions":[{"stem":"Which?",'
               '"choices":["One","Two","Three","Four"],'
               '"correct_index":0,"rationale":"One is correct."}]}')
    with app.state.database.session() as session:
        session.add(PublishedQuizModel(token=token, title="Local fixture", payload_json=payload))
    service = app.state.study_session_service
    for callback, value in ((service.load_quiz, token), (service.topics_for,
        QuestionKey(source="study_hub", product="lecture_quiz", question_id="q1"))):
        with pytest.raises(PermissionError):
            callback("other", value)
    with TestClient(app) as client:
        page = client.get(f"/study/sessions/new?quiz_token={token}")
        assert page.status_code == 200 and "Start session" in page.text
        csrf = client.cookies.get("study_hub_csrf")
        created = client.post("/study/sessions", json={"quiz_token": token},
                              headers={"X-CSRF-Token": csrf})
        assert created.status_code == 200
        path = created.json()["url"]
        content = client.get(path + "/content").json()
        assert "correct_choice_id" not in str(content)
        attempt = content["questions"][0]["attempt_id"]
        assert client.post(path + "/answers/" + attempt, json={"choice_id": "c1"},
            headers={"X-CSRF-Token": csrf}).json()["correct"] is True
        assert client.get("/study/progress/data").json()["summary"]["correct"] == 1
        assert client.post(f"/public/quizzes/{token}/answer",
            json={"question_id": "q1", "choice_id": "c2"}).status_code == 200
        assert len(app.state.question_bank.iter_attempts(learner_id="local-owner")) == 1
        owner_library = client.get("/studio/library/quizzes").text
        assert f"/study/sessions/new?quiz_token={token}" in owner_library
        assert "Study with progress" not in client.get("/public/quizzes").text
        with app.state.database.session() as session:
            session.add(PublishedQuizMediaModel(quiz_token=token, image_key="bad",
                path=str(tmp_path.parent / "outside.png"), sha256="a" * 64,
                media_type="image/png", width=1, height=1, alt_text="Image"))
        assert client.post("/study/sessions", json={"quiz_token": token},
            headers={"X-CSRF-Token": csrf}).status_code == 409
    app.state.database.close()


def test_block_app_callback_recovers_legacy_lecture_scope_without_mutation(tmp_path):
    import pytest

    from oms_hub.models import PublishedQuizModel
    from oms_hub.repositories import LectureInput
    from oms_hub.study_generation.domain import NativeQuiz, QuizChoice, QuizQuestion
    from oms_hub.study_generation.native_quiz import serialize_native_quiz

    app = create_app(Settings(_env_file=None, data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'hub.db'}", study_root=tmp_path / "study"))
    lecture_id = app.state.catalog_repository.upsert_lecture(
        LectureInput("Heme/Lymph", 3, 1, "Fixture", "", None))
    quiz = NativeQuiz("Fixture", (QuizQuestion("q1", "Which?",
        tuple(QuizChoice(f"c{i}", str(i)) for i in range(1, 5)), "c1", "Because one."),))
    with app.state.database.session() as session:
        session.add(PublishedQuizModel(token="legacy", lecture_id=lecture_id,
            title=quiz.title, payload_json=serialize_native_quiz(quiz)))
    service = app.state.study_block_service
    with pytest.raises(PermissionError):
        service.publications_for("other")
    catalog = service.catalog("local-owner")
    assert len(catalog.questions) == 1
    assert (catalog.questions[0].course, catalog.questions[0].exam_number) == ("heme/lymph", 3)
    with app.state.database.session() as session:
        stored = session.get(PublishedQuizModel, "legacy")
        assert stored.destination_subject == ""
        assert stored.payload_json == serialize_native_quiz(quiz)
    topics = app.state.study_topic_service
    assert topics.media_root == tmp_path.resolve()
    key = catalog.questions[0].key.question_id
    with TestClient(app) as client:
        page = client.get("/study/blocks?course=heme/lymph&exam=3&count=1")
        assert page.status_code == 200 and "1 questions selected" in page.text
        assert client.get("/study/blocks/questions/" + key).status_code == 200
        csrf = client.cookies.get("study_hub_csrf")
        started = client.post("/study/blocks", json={"course": "heme/lymph",
            "exam_numbers": [3], "count": 1, "selected_keys": [key]},
            headers={"X-CSRF-Token": csrf})
        assert started.status_code == 200
        content = client.get(started.json()["url"] + "/content").json()
        assert "Heme/Lymph" in content["questions"][0]["source_label"]
    app.state.database.close()
