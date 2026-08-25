from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from oms_hub.app import create_app
from oms_hub.config import Settings


def _client(tmp_path: Path) -> tuple[TestClient, str, str]:
    from oms_hub.knowledge.routes import build_knowledge_router

    from .test_service import _service

    service, revision_id, evidence, scope = _service(tmp_path)
    app = FastAPI()
    app.include_router(build_knowledge_router(service))
    client = TestClient(app, base_url="http://testserver")
    del service, scope
    return client, revision_id, evidence[0].evidence_id


def test_knowledge_routes_are_private_and_no_store(tmp_path: Path) -> None:
    client, _, evidence_id = _client(tmp_path)
    response = client.get(f"/api/v1/knowledge/evidence/{evidence_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "path" not in response.json()["preview"]


def test_knowledge_routes_map_malformed_missing_and_provenance_errors(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    assert client.get("/api/v1/knowledge/revisions/not-a-revision").status_code == 422
    assert client.get("/api/v1/knowledge/evidence/ev_missing").status_code == 404
    response = client.post(
        "/api/v1/knowledge/revisions/sr_aaaaaaaaaaaaaaaaaaaaaaaaaa/rebuild",
        headers={"content-length": "0"},
    )
    assert response.status_code == 409


def test_router_build_does_not_register_static_artifact_route(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    assert client.get("/artifacts/7/pdf").status_code == 404


def test_revision_route_exposes_only_safe_status_view(tmp_path: Path) -> None:
    client, revision_id, _ = _client(tmp_path)
    response = client.get(f"/api/v1/knowledge/revisions/{revision_id}")
    assert response.status_code == 200
    assert set(response.json()) == {"source_revision_id", "revision_state", "upload_eligible"}


def test_unexpected_service_error_is_generic_500(tmp_path: Path) -> None:
    from oms_hub.knowledge.routes import build_knowledge_router

    class Boom:
        def resolve_evidence(self, evidence_id: str) -> object:
            raise RuntimeError("internal secret")

    app = FastAPI()
    app.include_router(build_knowledge_router(Boom()))
    response = TestClient(app, raise_server_exceptions=False).get(
        "/api/v1/knowledge/evidence/ev_any"
    )
    assert response.status_code == 500
    assert "internal secret" not in response.text


def test_test_only_router_uses_application_auth_and_csrf(tmp_path: Path) -> None:
    from oms_hub.knowledge.routes import build_knowledge_router
    from .test_service import _service

    service, revision_id, evidence, _ = _service(tmp_path)
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            public_hostname="study.example.com",
        )
    )
    app.state.knowledge_service = service
    class FakeAccessVerifier:
        def verify(self, assertion: str) -> object:
            del assertion
            return object()

    app.state.access_verifier = FakeAccessVerifier()
    app.include_router(build_knowledge_router(service))
    with TestClient(app, base_url="https://study.example.com") as client:
        assert client.get(f"/api/v1/knowledge/evidence/{evidence[0].evidence_id}").status_code == 401
        authorized = client.get(
            f"/api/v1/knowledge/evidence/{evidence[0].evidence_id}",
            headers={"Cf-Access-Jwt-Assertion": "valid"},
        )
        assert authorized.status_code == 200
        # The app middleware rejects a state-changing request before the service.
        no_csrf = client.post(
            f"/api/v1/knowledge/revisions/{revision_id}/rebuild",
            headers={"Cf-Access-Jwt-Assertion": "valid", "content-length": "0"},
        )
        assert no_csrf.status_code == 403
        token = client.cookies.get("study_hub_csrf")
        with_csrf = client.post(
            f"/api/v1/knowledge/revisions/{revision_id}/rebuild",
            headers={
                "Cf-Access-Jwt-Assertion": "valid",
                "content-length": "0",
                "X-CSRF-Token": token or "",
                "Origin": "https://study.example.com",
            },
        )
        assert with_csrf.status_code == 409
