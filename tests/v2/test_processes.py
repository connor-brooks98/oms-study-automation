"""Local queue fences and private controls, without providers or live Anki."""

import json
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from oms_hub.db import Database
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.models import (
    BankImportModel,
    IngestionJobModel,
    ProcessControlModel,
    StudioRunArtifactModel,
    StudioRunModel,
    UploadBatchModel,
    UploadItemModel,
)
from oms_hub.processes import ProcessHeld, ProcessService, checkpoint, process_operation
from oms_hub.security.csrf import CsrfProtector
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.web.process_routes import router


@pytest.fixture
def database(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'processes.db'}")
    db.migrate()
    yield db
    db.close()


def upload(db, state="queued"):
    with db.session() as session:
        session.add(UploadBatchModel(id="batch", kind="slides", state=state))
        session.flush()
        session.add(
            UploadItemModel(
                id="upload",
                batch_id="batch",
                kind="slides",
                original_filename="A long lecture filename.pdf",
                staged_path="retained.pdf",
                sha256="a" * 64,
                size_bytes=10,
                state=state,
            )
        )
        session.flush()
        row = IngestionJobModel(upload_item_id="upload", action="process", state=state)
        session.add(row)
        session.flush()
        return str(row.id)


def studio(db, state="queued", gpt=False):
    with db.session() as session:
        session.add(
            StudioRunModel(
                id="run",
                subject="Cardio",
                subject_key="cardio",
                exam_number=1,
                destination_subject="Cardio",
                destination_subject_key="cardio",
                destination_exam_number=1,
                label="Quiz",
                label_key="quiz",
                prompt="retained",
                workflow_kind="lecture_generation" if gpt else "direct_import",
                backend="codex_subscription" if gpt else "notebooklm",
                state=state,
            )
        )
        session.flush()
        if gpt:
            session.add(
                StudioRunArtifactModel(
                    run_id="run",
                    artifact_key="gpt:settings",
                    signature_sha256="a" * 64,
                    payload_json=json.dumps({"owner_id": "owner"}),
                )
            )


def test_queued_pause_restart_and_remove_retain_upload(database):
    job_id = upload(database)
    service = ProcessService(database)
    item = service.action("ingestion", job_id, "pause", owner_id="owner")
    assert item["control_state"] == "paused"
    assert IngestionRepository(database).claim_next_job(datetime.now(UTC)) is None
    assert service.action("ingestion", job_id, "restart", owner_id="owner")["state"] == "queued"
    assert IngestionRepository(database).claim_next_job(datetime.now(UTC)) is not None
    item = service.action("ingestion", job_id, "remove", owner_id="owner")
    assert item["control_state"] == "pause_requested" and not item["hidden"]
    with pytest.raises(ProcessHeld), process_operation(database, "ingestion", job_id):
        pytest.fail("held work must not start")
    assert service.list("owner")["items"] == []
    with database.session() as session:
        assert session.get(UploadItemModel, "upload").staged_path == "retained.pdf"
        assert session.get(IngestionJobModel, int(job_id)) is not None
        control = session.get(ProcessControlModel, ("ingestion", job_id))
        assert [e["action"] for e in json.loads(control.events_json)] == [
            "pause",
            "restart",
            "remove",
        ]


def test_active_pause_waits_for_checkpoint_and_prevents_filing(database):
    job_id = upload(database, "processing")
    service = ProcessService(database)
    steps = []
    with pytest.raises(ProcessHeld), process_operation(database, "ingestion", job_id):
        steps.append("converted")
        item = service.action("ingestion", job_id, "pause", owner_id="owner")
        assert item["control_label"] == "Pausing after current operation"
        assert not item["actions"]["restart"]["enabled"]
        checkpoint()
        steps.append("filed")
    item = service.list("owner")["items"][0]
    assert steps == ["converted"]
    assert item["control_state"] == "paused" and item["actions"]["restart"]["enabled"]
    service.action("ingestion", job_id, "restart", owner_id="owner")
    assert IngestionRepository(database).claim_next_job(datetime.now(UTC)) is not None


def test_completion_wins_if_operation_finishes_before_pause(database):
    job_id = upload(database, "processing")
    service = ProcessService(database)
    with process_operation(database, "ingestion", job_id):
        service.action("ingestion", job_id, "pause", owner_id="owner")
        with database.session() as session:
            session.get(IngestionJobModel, int(job_id)).state = "complete"
    item = service.list("owner")["items"][0]
    assert item["control_state"] == "terminal" and item["control_label"] == "Complete"
    assert not item["actions"]["restart"]["enabled"]


def test_import_hold_keeps_durable_run_and_excludes_claim(database):
    studio(database)
    service = ProcessService(database)
    service.action("studio", "run", "pause", owner_id="owner")
    assert StudioRepository(database).claim_next_run() is None
    service.action("studio", "run", "restart", owner_id="owner")
    assert StudioRepository(database).claim_next_run().id == "run"


def test_gpt_owner_and_retained_attempt_guard(database):
    studio(database, gpt=True)
    service = ProcessService(database)
    assert service.list("other")["items"] == []
    with pytest.raises(KeyError):
        service.action("studio", "run", "pause", owner_id="other")
    item = service.action("studio", "run", "pause", owner_id="owner")
    assert item["state"] == "interrupted"
    with database.session() as session:
        session.add(
            StudioRunArtifactModel(
                run_id="run",
                artifact_key="gpt:attempt:1",
                signature_sha256="b" * 64,
                payload_json=json.dumps({"phase": "dispatching"}),
            )
        )
    with pytest.raises(ValueError):
        service.action("studio", "run", "restart", owner_id="owner")
    assert StudioRepository(database).claim_next_run() is None


def test_owner_csrf_and_hidden_bank_receipts(database):
    job_id = upload(database)
    with database.session() as session:
        session.add(
            BankImportModel(
                id="bank",
                learner_id="other",
                source="vendor",
                product="qbank",
                export_id="export",
                digest="d" * 64,
                provenance_json="{}",
            )
        )
    app = FastAPI()
    app.state.process_service = ProcessService(database)
    app.state.csrf = CsrfProtector(b"a" * 32)

    @app.middleware("http")
    async def owner(request: Request, call_next):
        request.state.study_owner_id = request.headers.get("test-owner")
        return await call_next(request)

    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/api/processes").status_code == 403
        client.headers["test-owner"] = "owner"
        assert len(client.get("/api/processes").json()["items"]) == 1
        url = f"/api/processes/ingestion/{job_id}/pause"
        assert client.post(url).status_code == 403
        token = app.state.csrf.issue()
        client.cookies.set("study_hub_csrf", token)
        assert client.post(url, headers={"X-CSRF-Token": token}).status_code == 200
        assert (
            client.post(
                "/api/processes/bank/bank/remove", headers={"X-CSRF-Token": token}
            ).status_code
            == 404
        )


def test_migration42_adds_only_control_table_and_revalidates(database):
    with database.engine.begin() as connection:
        connection.execute(text("DROP TABLE process_controls"))
        connection.execute(text("UPDATE schema_version SET version=41"))
    database.migrate()
    with database.session() as session:
        assert session.scalars(select(ProcessControlModel)).all() == []
    database.migrate()


def test_gpt_manifest_projects_lecture_and_malformed_owner_is_hidden(database):
    studio(database, gpt=True)
    with database.session() as session:
        session.add(
            StudioRunArtifactModel(
                run_id="run",
                artifact_key="gpt:manifest",
                signature_sha256="f" * 64,
                payload_json='{"lecture_id":123}',
            )
        )
    assert ProcessService(database).list("owner")["items"][0]["lecture_id"] == 123
    with database.session() as session:
        settings = session.scalar(
            select(StudioRunArtifactModel).where(
                StudioRunArtifactModel.artifact_key == "gpt:settings"
            )
        )
        settings.payload_json = "broken"
    assert ProcessService(database).list("owner")["items"] == []


def test_ingestion_worker_acknowledges_at_boundary_without_failure(database):
    from types import SimpleNamespace

    from oms_hub.ingestion.worker import IngestionWorker

    job_id = upload(database)
    service = ProcessService(database)
    completed = []

    def process(item_id):
        service.action("ingestion", job_id, "pause", owner_id="owner")
        checkpoint()
        completed.append(item_id)

    pipeline = SimpleNamespace(process=process)
    worker = IngestionWorker(IngestionRepository(database), pipeline, pipeline)
    assert worker.run_once()
    assert not completed
    item = service.list("owner")["items"][0]
    assert item["control_state"] == "paused" and item["error"] is None


def test_source_hold_fences_queued_operations_but_never_reconciliation(database):
    from oms_hub.models import StudioSourceModel, StudioSourceOperationModel

    with database.session() as session:
        session.add(
            StudioSourceModel(
                id="source",
                subject="Cardio",
                subject_key="cardio",
                exam_number=1,
                source_type="text",
                title="Retained source",
                state="attaching",
            )
        )
        session.flush()
        session.add(
            StudioSourceOperationModel(
                id="operation", source_id="source", operation_kind="add", state="queued"
            )
        )
    service = ProcessService(database)
    item = service.action("source", "source", "pause", owner_id="owner")
    assert item["control_state"] == "paused"
    assert StudioRepository(database).claim_next_source_operation() is None
    with database.session() as session:
        session.get(StudioSourceOperationModel, "operation").state = "reconciling"
    assert StudioRepository(database).claim_next_source_operation() is not None
    item = next(i for i in service.list("owner")["items"] if i["family"] == "source")
    assert not any(a["enabled"] for a in item["actions"].values())


def test_anki_claim_hold_preserves_stage_and_disables_live_apply(database):
    from oms_hub.anki.models import AnkiCurationJobModel
    from oms_hub.anki.repository import AnkiCurationRepository
    from oms_hub.models import LectureModel

    job_id = "11111111-1111-4111-8111-111111111111"
    with database.session() as session:
        lecture = LectureModel(subject="Cardio", exam_number=1, lecture_number=1, topic="Heart")
        session.add(lecture)
        session.flush()
        session.add(
            AnkiCurationJobModel(
                id=job_id,
                lecture_id=lecture.id,
                target_deck="Retained",
                target_tag="retained",
                index_snapshot_id="snapshot",
                instruction_sha256="a" * 64,
                lcl_prompt_version="v1",
                judgment_rubric_version="v1",
                gap_prompt_version="v1",
            )
        )
    service = ProcessService(database)
    service.action("anki", job_id, "pause", owner_id="owner")
    assert AnkiCurationRepository(database).claim_next_job(datetime.now(UTC)) is None
    service.action("anki", job_id, "restart", owner_id="owner")
    with database.session() as session:
        row = session.get(AnkiCurationJobModel, job_id)
        assert row.state == "queued" and row.index_snapshot_id == "snapshot"
        row.state = "applying_local"
    item = next(i for i in service.list("owner")["items"] if i["family"] == "anki")
    assert not any(a["enabled"] for a in item["actions"].values())
    with pytest.raises(ValueError, match="live changes"):
        service.action("anki", job_id, "remove", owner_id="owner")


def test_startup_recovery_acknowledges_hold_without_requeue(database):
    job_id = upload(database, "processing")
    service = ProcessService(database)
    service.action("ingestion", job_id, "pause", owner_id="owner")
    IngestionRepository(database).recover_interrupted_jobs()
    card = service.list("owner")["items"][0]
    assert card["state"] == "processing" and card["control_state"] == "paused"
    assert IngestionRepository(database).claim_next_job(datetime.now(UTC)) is None
    service.action("ingestion", job_id, "restart", owner_id="owner")
    assert IngestionRepository(database).claim_next_job(datetime.now(UTC)) is not None


def test_existing_gpt_resume_clears_acknowledged_process_pause(database):
    studio(database, gpt=True)
    service = ProcessService(database)
    service.action("studio", "run", "pause", owner_id="owner")
    StudioRepository(database).control_gpt_run("run", owner_id="owner", action="resume")
    assert StudioRepository(database).claim_next_run() is not None


def test_interrupted_import_hold_does_not_authorize_paid_replay(database):
    studio(database, state="running")
    service = ProcessService(database)
    service.action("studio", "run", "pause", owner_id="owner")
    StudioRepository(database).recover_interrupted_jobs()
    item = service.list("owner")["items"][0]
    assert item["control_state"] == "paused"
    assert not item["actions"]["restart"]["enabled"]
    with pytest.raises(ValueError, match="review flow"):
        service.action("studio", "run", "restart", owner_id="owner")


def test_failed_direct_import_requires_existing_review_flow(database):
    studio(database, state="failed")
    with pytest.raises(ValueError, match="review flow"):
        ProcessService(database).action("studio", "run", "restart", owner_id="owner")


def test_pause_before_optional_filing_hook_never_queues_followup(database):
    from types import SimpleNamespace

    from oms_hub.ingestion.worker import IngestionWorker

    job_id = upload(database)
    service = ProcessService(database)
    followups = []

    def process(item_id):
        service.action("ingestion", job_id, "pause", owner_id="owner")
        return item_id

    pipeline = SimpleNamespace(process=process)
    worker = IngestionWorker(
        IngestionRepository(database), pipeline, pipeline, on_filed=followups.append
    )
    assert worker.run_once()
    assert not followups
    assert service.list("owner")["items"][0]["control_state"] == "paused"


def test_gpt_restart_failure_preserves_hold_and_does_not_acknowledge_other_worker(
    database, monkeypatch
):
    studio(database, gpt=True)
    service = ProcessService(database)
    service.action("studio", "run", "pause", owner_id="owner")
    original = StudioRepository.control_gpt_run

    def interleaved(repository, run_id, *, owner_id, action):
        with database.session() as session:
            hold = session.get(ProcessControlModel, ("studio", "run"))
            assert hold.requested_action == "pause" and hold.acknowledged_at
        # Another explicit resume succeeds and starts its worker first.
        original(repository, run_id, owner_id=owner_id, action="resume")
        assert repository.claim_next_run() is not None
        return original(repository, run_id, owner_id=owner_id, action=action)

    monkeypatch.setattr(StudioRepository, "control_gpt_run", interleaved)
    with pytest.raises(ValueError, match="current state"):
        service.action("studio", "run", "restart", owner_id="owner")
    with database.session() as session:
        hold = session.get(ProcessControlModel, ("studio", "run"))
        assert hold.requested_action is None and hold.acknowledged_at is None
        assert session.get(StudioRunModel, "run").state == "running"
    item = service.list("owner")["items"][0]
    assert item["active"] and item["control_state"] == "running"


def test_gpt_pause_after_claim_before_dispatch_acknowledges_stopped_worker(database, monkeypatch):
    from types import SimpleNamespace

    from oms_hub.study_generation.studio_worker import StudioWorker

    studio(database, gpt=True)
    service = ProcessService(database)
    repository = StudioRepository(database)
    claim = repository.claim_next_run

    def claim_then_pause():
        run = claim()
        service.action("studio", "run", "pause", owner_id="owner")
        return run

    monkeypatch.setattr(repository, "claim_next_run", claim_then_pause)
    worker = StudioWorker(
        repository,
        None,
        None,
        None,
        gpt_worker=SimpleNamespace(run=lambda run: pytest.fail("must not dispatch")),
    )
    assert worker.run_once()
    control = repository.run_artifact("run", "gpt:control")
    assert json.loads(control.payload_json)["worker_stopping"] is False
    item = service.list("owner")["items"][0]
    assert item["control_state"] == "paused" and item["actions"]["restart"]["enabled"]
