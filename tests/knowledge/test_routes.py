from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path: Path) -> TestClient:
    from .test_service import _service
    from oms_hub.knowledge.routes import build_knowledge_router

    service, revision_id, evidence, scope = _service(tmp_path)
    app = FastAPI()
    app.include_router(build_knowledge_router(service))
    client = TestClient(app, base_url="http://testserver")
    client.service = service  # type: ignore[attr-defined]
    client.revision_id = revision_id  # type: ignore[attr-defined]
    client.evidence_id = evidence[0].evidence_id  # type: ignore[attr-defined]
    client.scope = scope  # type: ignore[attr-defined]
    return client


def test_knowledge_routes_are_private_and_no_store(tmp_path: Path):
    client = _client(tmp_path)
    response = client.get(f"/api/v1/knowledge/evidence/{client.evidence_id}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "path" not in response.json()["preview"]


def test_knowledge_routes_map_malformed_missing_and_provenance_errors(tmp_path: Path):
    client = _client(tmp_path)
    assert client.get("/api/v1/knowledge/revisions/not-a-revision").status_code == 422
    assert client.get("/api/v1/knowledge/evidence/ev_missing").status_code == 404
    response = client.post(
        "/api/v1/knowledge/revisions/sr_missing/rebuild",
        headers={"content-length": "0"},
    )
    assert response.status_code == 409


def test_router_build_does_not_register_static_artifact_route(tmp_path: Path):
    client = _client(tmp_path)
    assert client.get("/artifacts/7/pdf").status_code == 404
