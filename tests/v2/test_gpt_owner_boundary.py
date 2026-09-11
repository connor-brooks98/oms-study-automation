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
