from fastapi import FastAPI
from fastapi.testclient import TestClient


def csrf_client(app: FastAPI) -> TestClient:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    token = client.cookies.get("study_hub_csrf")
    assert token is not None
    client.headers.update({"X-CSRF-Token": token})
    return client
