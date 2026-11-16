import json

import pytest
from fastapi.testclient import TestClient

from oms_hub.app import create_app
from oms_hub.config import Settings
from oms_hub.db import Database
from oms_hub.models import (
    NotebookScopeLeaseModel,
    ProcessControlModel,
    StudioSourceModel,
    StudioSourceOperationModel,
)
from oms_hub.processes import ProcessService
from oms_hub.study_generation.notebook_connection import NotebookConnectionService
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_worker import StudioWorker


@pytest.fixture
def repository(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    db.migrate()
    yield StudioRepository(db)
    db.close()


def source(repository, name, **changes):
    values = dict(
        id=name,
        subject="Test",
        subject_key="test",
        exam_number=1,
        source_type="text",
        title=name,
        purpose="notebook",
        state="needs_review",
        diagnostic_source="notebook_upload_unavailable",
        error="Original reason",
        attempts=0,
    )
    values.update(changes)
    with repository.database.session() as session:
        session.add(StudioSourceModel(**values))
    return name


def test_retirement_is_predicate_checked_idempotent_and_preserves_audit(repository):
    source(repository, "eligible")
    source(repository, "attempted", attempts=1)
    source(repository, "different-error", diagnostic_source="network")
    source(repository, "import", purpose="local_import")
    source(repository, "remote", remote_source_id="remote-source")
    source(repository, "notebook", remote_notebook_id="remote-notebook")
    source(repository, "claimed", state="attaching")
    source(repository, "operation")
    with repository.database.session() as session:
        session.add(
            StudioSourceOperationModel(
                id="past", source_id="operation", operation_kind="add", state="failed"
            )
        )
    assert repository.retire_abandoned_notebook_sources() == 1
    assert repository.retire_abandoned_notebook_sources() == 0
    assert repository.get("eligible").state.value == "skipped"
    assert repository.get("eligible").remote_source_id is None
    with repository.database.session() as session:
        control = session.get(ProcessControlModel, ("source", "eligible"))
        (event,) = json.loads(control.events_json)
        assert event["previous_state"] == "needs_review"
        assert event["previous_diagnostic_source"] == "notebook_upload_unavailable"
        assert event["previous_error"] == "Original reason"
        assert event["action"] == "retired_legacy_notebook"
        assert event["at"]
        assert control.hidden_at is None
    items = ProcessService(repository.database).list("owner")["items"]
    retired = next(item for item in items if item["job_id"] == "eligible")
    assert retired["control_state"] == "terminal"
    assert not retired["actions"]["restart"]["enabled"]
    for name in ("attempted", "different-error", "import", "remote", "notebook", "operation"):
        assert repository.get(name).state.value == "needs_review"
    assert repository.get("claimed").state.value == "attaching"


def test_any_scope_lease_record_blocks_retirement(repository):
    source(repository, "leased")
    with repository.database.session() as session:
        session.add(
            NotebookScopeLeaseModel(
                subject_key="test",
                exam_number=1,
                owner_kind="source",
                owner_id="leased",
                lease_expires_at="2000-01-01T00:00:00+00:00",
            )
        )
    assert repository.retire_abandoned_notebook_sources() == 0


def test_worker_never_claims_or_recovers_legacy_remote_source_work(repository):
    source(repository, "queued", state="pending", diagnostic_source=None, payload_path="/unused")
    source(repository, "active", state="attaching", diagnostic_source=None, attempts=1)
    with repository.database.session() as session:
        session.add(
            StudioSourceOperationModel(
                id="active-op",
                source_id="active",
                operation_kind="add",
                state="executing",
                lease_owner="old-worker",
                lease_expires_at="2000-01-01",
            )
        )
    worker = StudioWorker(repository, object(), object(), object())
    worker.recover_interrupted_jobs()
    assert not worker.run_once()
    assert repository.get("queued").attempts == 0
    with repository.database.session() as session:
        operation = session.get(StudioSourceOperationModel, "active-op")
        assert operation.state == "executing"
        assert operation.lease_owner == "old-worker"


@pytest.mark.parametrize("method", ["test", "start_interactive", "require_live"])
def test_retired_connection_never_touches_auth_or_storage(method):
    connection = NotebookConnectionService(object(), object())
    result = getattr(connection, method)()
    assert result.state == "retired"


def test_app_detaches_ingestion_hook_and_removes_legacy_ui(tmp_path):
    app = create_app(
        Settings(_env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'app.db'}")
    )
    try:
        assert app.state.ingestion_worker.on_filed is None
        client = TestClient(app)
        for endpoint in ("connect", "test"):
            assert client.post(f"/settings/notebook/{endpoint}").status_code == 410
        settings = client.get("/settings").text
        assert "data-notebook-card" not in settings
        page = client.get("/studio").text
        assert "workflow-panel-generate" not in page
        assert 'data-import-source-type="file"' in page
        assert 'data-import-source-type="text"' in page
        assert 'data-import-source-type="url"' in page
        assert "data-import-notebook" not in page
        token = client.cookies.get("study_hub_csrf")
        fields = {
            "subject": "Test",
            "exam_number": 1,
            "title": "Retired source",
            "text": "source",
            "url": "https://example.invalid/source",
        }
        for endpoint in ("text", "url", "image-url", "file"):
            response = client.post(
                f"/studio/sources/{endpoint}",
                data=fields,
                files={"file": ("source.txt", b"Synthetic")} if endpoint == "file" else None,
                headers={"X-CSRF-Token": token},
            )
            assert response.status_code == 410
        assert app.state.studio_repository.list_sources() == []
    finally:
        app.state.database.close()


@pytest.mark.parametrize(
    "method,args",
    [
        ("ensure_notebook", ("Course", 1)),
        ("prepare_studio_source_add", ("Course", 1)),
        ("add_studio_source_to_notebook", ("remote", "text", "source")),
        ("list_studio_source_ids", ("remote",)),
        ("delete_studio_source", ("remote", "source")),
    ],
)
def test_gateway_stops_before_remote_access(method, args):
    from oms_hub.study_generation.notebook import StoredNotebookLMGateway
    from oms_hub.study_generation.notebook_errors import NotebookGatewayError

    gateway = object.__new__(StoredNotebookLMGateway)
    with pytest.raises(NotebookGatewayError, match="retired"):
        getattr(gateway, method)(*args)
