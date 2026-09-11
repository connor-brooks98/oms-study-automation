from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from oms_hub.security.csrf import CsrfProtector
from oms_hub.study_chat.routes import router
from oms_hub.study_chat.service import ChatService

from .test_service import FakeClient


def client_for(repo):
    app = FastAPI()
    app.state.test_owner = "owner"
    app.state.csrf = CsrfProtector(b"x" * 32)
    app.state.study_chat_service = ChatService(repo, FakeClient(), model="chosen")

    @app.middleware("http")
    async def owner(request: Request, call_next):
        if app.state.test_owner:
            request.state.study_owner_id = app.state.test_owner
        return await call_next(request)

    app.include_router(router)
    client = TestClient(app)
    token = app.state.csrf.issue()
    client.cookies.set("study_hub_csrf", token)
    client.headers["X-CSRF-Token"] = token
    return client, app


def test_routes_require_owner_and_csrf_and_ignore_client_identity(setup):
    repo, _, _ = setup
    client, app = client_for(repo)
    with client:
        app.state.test_owner = None
        assert client.get("/study/chat", headers={"X-Owner": "owner"}).status_code == 401
        app.state.test_owner = "owner"
        assert client.get("/study/chat").status_code == 200
        data = {"mode": "general", "revision_ids": []}
        assert (
            client.post("/study/chat/conversations", json=data | {"owner_id": "other"}).status_code
            == 422
        )
        token = client.headers.pop("X-CSRF-Token")
        assert client.post("/study/chat/conversations", json=data).status_code == 403
        client.headers["X-CSRF-Token"] = token
        cid = client.post("/study/chat/conversations", json=data).json()["conversation_id"]
        app.state.test_owner = "other"
        assert client.get(f"/study/chat/conversations/{cid}").status_code == 403
        assert client.post(f"/study/chat/conversations/{cid}/clear").status_code == 403


def test_answer_citations_and_reload_are_owner_scoped(setup):
    from uuid import uuid4

    repo, _, _ = setup
    client, app = client_for(repo)
    with client:
        cid = client.post(
            "/study/chat/conversations", json={"mode": "lecture", "revision_ids": [1]}
        ).json()["conversation_id"]
        rid = str(uuid4())
        response = client.post(
            "/study/chat/answer",
            json={"conversation_id": cid, "request_id": rid, "question": "iron"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "completed" and data["answer"]["status"] == "answered"
        url = data["citations"][0]["url"]
        assert url.startswith(f"/study/chat/requests/{rid}/citations/")
        excerpt = client.get(url)
        assert excerpt.status_code == 200 and "Iron stores" in excerpt.text
        assert excerpt.headers["content-type"].startswith("text/plain")
        assert (
            client.get(f"/study/chat/conversations/{cid}").json()["requests"][0]["request_id"]
            == rid
        )
        assert "private, no-store" == response.headers["cache-control"]
        app.state.test_owner = "other"
        assert client.get(url).status_code == 403
        assert client.post(f"/study/chat/requests/{rid}/cancel").status_code == 403


def test_page_escapes_source_titles_and_errors_do_not_leak(setup):
    from oms_hub.models import LectureModel

    repo, database, _ = setup
    with database.session() as session:
        session.get(LectureModel, 1).topic = '<script>alert("bad")</script>'
    client, _ = client_for(repo)
    with client:
        page = client.get("/study/chat")
        assert "<script>alert" not in page.text
        assert "&lt;script&gt;alert" in page.text
        denied = client.post(
            "/study/chat/conversations", json={"mode": "lecture", "revision_ids": [999]}
        )
        assert denied.status_code == 403
        assert str(database.engine.url) not in denied.text


def test_unconfigured_page_is_readable_and_does_not_construct_a_client(setup):
    repo, _, _ = setup
    client, app = client_for(repo)
    app.state.study_chat_service = None
    with client:
        response = client.get("/study/chat")
        assert response.status_code == 200
        assert "Chat is unavailable" in response.text
        assert "data-chat-form" not in response.text
        assert client.post("/study/chat/conversations", json={"mode": "general"}).status_code == 503
