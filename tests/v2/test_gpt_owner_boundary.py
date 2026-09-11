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
