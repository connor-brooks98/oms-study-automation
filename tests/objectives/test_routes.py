from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from oms_hub.objectives.extraction import ProposedObjective
from oms_hub.objectives.routes import build_objective_router
from oms_hub.objectives.service import (
    ObjectiveProposalDisposition,
    ObjectiveProposalRecord,
)


def _record() -> ObjectiveProposalRecord:
    proposal = ProposedObjective(
        observable_verb="differentiate",
        concept="heparin-induced thrombocytopenia",
        description="Differentiate HIT from other thrombocytopenias.",
        course_id="heme",
        exam_id="exam-2",
        lecture_ids=("lecture-13",),
        source_revision_ids=("revision-1",),
        evidence_ids=("evidence-1",),
    )
    return ObjectiveProposalRecord(
        proposal=proposal,
        disposition=ObjectiveProposalDisposition.PENDING,
        created_at="2026-08-26T14:00:00+00:00",
        updated_at="2026-08-26T14:00:00+00:00",
    )


class Service:
    def __init__(self) -> None:
        self.record = _record()
        self.calls: list[tuple[str, object]] = []

    def extract(self, source_revision_ids: tuple[str, ...]) -> tuple[ObjectiveProposalRecord, ...]:
        self.calls.append(("extract", source_revision_ids))
        return (self.record,)

    def list_proposals(self) -> tuple[ObjectiveProposalRecord, ...]:
        self.calls.append(("list", None))
        return (self.record,)

    def approve(self, objective_id: str) -> ObjectiveProposalRecord:
        self.calls.append(("approve", objective_id))
        return self.record

    def merge(self, objective_id: str, target_id: str) -> ObjectiveProposalRecord:
        self.calls.append(("merge", (objective_id, target_id)))
        return self.record

    def retire(self, objective_id: str) -> ObjectiveProposalRecord:
        self.calls.append(("retire", objective_id))
        return self.record


def _client(service: object) -> TestClient:
    app = FastAPI()
    app.include_router(build_objective_router(service))
    return TestClient(app, raise_server_exceptions=False)


def test_objective_routes_expose_review_workflow_and_private_cache_headers() -> None:
    service = Service()
    client = _client(service)
    objective_id = service.record.proposal.proposal_id

    responses = (
        client.post(
            "/api/v1/objectives/extract",
            json={"source_revision_ids": ["revision-1"]},
        ),
        client.get("/api/v1/objectives"),
        client.post(f"/api/v1/objectives/{objective_id}/approve"),
        client.post(
            f"/api/v1/objectives/{objective_id}/merge",
            json={"target_objective_id": "objective-target"},
        ),
        client.post(f"/api/v1/objectives/{objective_id}/retire"),
    )

    assert all(response.status_code == 200 for response in responses)
    assert all(response.headers["cache-control"] == "private, no-store" for response in responses)
    assert service.calls == [
        ("extract", ("revision-1",)),
        ("list", None),
        ("approve", objective_id),
        ("merge", (objective_id, "objective-target")),
        ("retire", objective_id),
    ]
    assert responses[0].json()["objectives"][0]["status"] == "pending"


def test_objective_routes_validate_inputs_and_map_review_conflicts() -> None:
    class Broken(Service):
        def approve(self, objective_id: str) -> ObjectiveProposalRecord:
            raise KeyError(objective_id)

        def retire(self, objective_id: str) -> ObjectiveProposalRecord:
            raise ValueError("objective is already merged")

    client = _client(Broken())

    empty = client.post(
        "/api/v1/objectives/extract",
        json={"source_revision_ids": []},
    )
    assert empty.status_code == 422
    assert client.post("/api/v1/objectives/missing/approve").status_code == 404
    assert client.post("/api/v1/objectives/merged/retire").status_code == 409


def test_unexpected_route_failure_does_not_leak_internal_details() -> None:
    class Boom(Service):
        def list_proposals(self) -> tuple[ObjectiveProposalRecord, ...]:
            raise RuntimeError("private database detail")

    response = _client(Boom()).get("/api/v1/objectives")

    assert response.status_code == 500
    assert "private database detail" not in response.text


def test_objective_router_inherits_application_auth_and_csrf(tmp_path: Path) -> None:
    from oms_hub.app import create_app
    from oms_hub.config import Settings

    class AccessVerifier:
        def verify(self, assertion: str) -> object:
            del assertion
            return object()

    service = Service()
    app = create_app(
        Settings(
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'hub.db'}",
            public_hostname="study.example.com",
        )
    )
    app.state.access_verifier = AccessVerifier()
    app.state.objective_service = service
    app.include_router(build_objective_router(service))
    with TestClient(app, base_url="https://study.example.com") as client:
        assert client.get("/api/v1/objectives").status_code == 401
        authorized = client.get(
            "/api/v1/objectives",
            headers={"Cf-Access-Jwt-Assertion": "valid"},
        )
        assert authorized.status_code == 200
        objective_id = service.record.proposal.proposal_id
        no_csrf = client.post(
            f"/api/v1/objectives/{objective_id}/approve",
            headers={"Cf-Access-Jwt-Assertion": "valid", "content-length": "0"},
        )
        assert no_csrf.status_code == 403
        token = client.cookies.get("study_hub_csrf")
        approved = client.post(
            f"/api/v1/objectives/{objective_id}/approve",
            headers={
                "Cf-Access-Jwt-Assertion": "valid",
                "content-length": "0",
                "X-CSRF-Token": token or "",
                "Origin": "https://study.example.com",
            },
        )
        assert approved.status_code == 200


def test_objective_router_is_not_registered_on_production_app(tmp_path: Path) -> None:
    from oms_hub.app import create_app
    from oms_hub.config import Settings

    app = create_app(
        Settings(
            data_dir=tmp_path,
            database_url="sqlite://",
        )
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/objectives").status_code == 404
