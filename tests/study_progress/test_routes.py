from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from oms_hub.security.csrf import CsrfProtector
from oms_hub.study_progress.routes import router
from oms_hub.study_progress.service import ProgressService
from oms_hub.web.public_quiz_routes import router as public_router

from .test_sessions import sessions  # noqa: F401


def client_for(service, bank, current):
    app = FastAPI()
    app.state.test_owner = "owner"
    app.state.csrf = CsrfProtector(b"x" * 32)
    app.state.study_session_service = service
    app.state.study_progress_service = ProgressService(bank)
    app.state.generation_repository = SimpleNamespace(published_quiz=lambda token: current[0])
    app.include_router(router)
    app.include_router(public_router)

    @app.middleware("http")
    async def owner(request: Request, call_next):
        if app.state.test_owner:
            request.state.study_owner_id = app.state.test_owner
        return await call_next(request)

    client = TestClient(app)
    token = app.state.csrf.issue()
    client.cookies.set("study_hub_csrf", token)
    client.headers["X-CSRF-Token"] = token
    return client, app


def test_private_answer_replay_conflict_and_public_does_not_write(sessions):  # noqa: F811
    service, bank, _, current = sessions
    client, app = client_for(service, bank, current)
    with client:
        created = client.post("/study/sessions", json={"quiz_token": "quiz"})
        assert created.status_code == 200
        path = created.json()["url"]
        content = client.get(path + "/content")
        assert content.headers["cache-control"] == "private, no-store"
        assert "correct_choice_id" not in content.text
        assert "Secret objective" not in content.text
        attempt = content.json()["questions"][0]["attempt_id"]
        url = path + "/answers/" + attempt
        answer = {"choice_id": "c1"}
        response = client.post(url, json=answer)
        assert response.status_code == 200 and response.json()["correct"] is True
        assert client.post(url, json=answer).json() == response.json()
        assert len(bank.iter_attempts(learner_id="owner")) == 1
        assert client.post(url, json={"choice_id": "c2"}).status_code == 409
        assert client.post(url, json=answer | {"correct": True}).status_code == 422
        assert client.post(url, json=answer | {"elapsed_ms": -1}).status_code == 422
        assert client.post(url, json=answer | {"owner_id": "other"}).status_code == 422
        assert client.get(path + "/content").json()["questions"][0]["feedback"]["correct"]
        app.state.test_owner = None
        for private in (path, path + "/content", "/study/progress", "/study/progress/data"):
            assert client.get(private).status_code == 401
        assert client.post(url, json=answer).status_code == 401
        public = client.post(
            "/public/quizzes/quiz/answer", json={"question_id": "q1", "choice_id": "c1"}
        )
        assert public.status_code == 200 and public.json()["correct"] is True
        assert len(bank.iter_attempts(learner_id="owner")) == 1


def test_csrf_owner_isolation_progress_and_start_form(sessions):  # noqa: F811
    service, bank, _, current = sessions
    client, app = client_for(service, bank, current)
    with client:
        page = client.get("/study/sessions/new?quiz_token=quiz")
        assert "Start session" in page.text
        token = client.headers.pop("X-CSRF-Token")
        assert client.post("/study/sessions", json={"quiz_token": "quiz"}).status_code == 403
        started = client.post(
            "/study/sessions/start",
            data={"quiz_token": "quiz", "csrf_token": token},
            follow_redirects=False,
        )
        assert started.status_code == 303
        path = started.headers["location"]
        assert 'data-personal-session="true"' in client.get(path).text
        client.headers["X-CSRF-Token"] = token
        attempt = client.get(path + "/content").json()["questions"][0]["attempt_id"]
        assert (
            client.post(path + "/answers/" + attempt, json={"choice_id": "c2"}).status_code == 200
        )
        assert client.get("/study/progress/data").json()["summary"]["incorrect"] == 1
        assert "No known attempt dates" not in client.get("/study/progress").text
        assert "Category sources" in client.get("/study/progress").text
        app.state.test_owner = "other"
        assert client.get(path + "/content").status_code == 403
        assert client.post(path + "/close").status_code == 403
        assert client.get("/study/progress/data").json()["sample_count"] == 0
