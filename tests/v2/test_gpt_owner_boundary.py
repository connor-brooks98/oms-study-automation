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
