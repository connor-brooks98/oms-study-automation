from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from reportlab.pdfgen.canvas import Canvas

from oms_hub.db import Database
from oms_hub.files.atomic import sha256_file
from oms_hub.ingestion.domain import IngestionJob, UploadKind
from oms_hub.ingestion.repository import IngestionRepository
from oms_hub.ingestion.worker import IngestionWorker
from oms_hub.models import StudyRevisionModel, UploadBatchModel, UploadItemModel
from oms_hub.repositories import CatalogRepository, LectureInput
from oms_hub.study_generation.notebook import StoredNotebookLMGateway
from oms_hub.study_generation.notebook_errors import NotebookGatewayError
from oms_hub.study_generation.notebook_sync import LectureNotebookSync
from oms_hub.study_generation.studio_domain import StudioSourceState
from oms_hub.study_generation.studio_repository import StudioRepository
from oms_hub.study_generation.studio_worker import StudioWorker


@pytest.mark.parametrize("method,args", [
    ("ask", (None, None, None)), ("generate", (None,) * 6),
    ("ask_studio", (None,) * 4), ("answer_studio_question", (None,) * 4),
])
def test_inference_gateway_stops_before_client_credentials_or_remote_mutations(method, args):
    # No instance dependencies exist: touching storage, repository or a client fails the test.
    gateway = object.__new__(StoredNotebookLMGateway)
    with pytest.raises(NotebookGatewayError, match="upload-only") as failure:
        getattr(gateway, method)(*args)
    assert not failure.value.retryable


@pytest.fixture
def filed(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'hub.db'}")
    database.migrate()
    lecture_id = CatalogRepository(database).upsert_lecture(
        LectureInput("Synthetic", 1, 1, "Fixture", "Teacher", None)
    )
    for kind, suffix in (("slides", ".pdf"), ("transcripts", ".txt")):
        path = tmp_path / (kind + suffix)
        if suffix == ".pdf":
            canvas = Canvas(str(path))
            canvas.drawString(40, 700, "Synthetic source only")
            canvas.save()
        else:
            path.write_text("Synthetic cleaned transcript only.")
        digest = sha256_file(path)
        with database.session() as session:
            session.add(UploadBatchModel(id=kind, kind=kind, state="complete"))
            session.flush()
            session.add(UploadItemModel(
                id=kind, batch_id=kind, kind=kind, original_filename=path.name,
                staged_path=str(path), sha256=digest, size_bytes=path.stat().st_size,
                state="complete", lecture_id=lecture_id,
            ))
            session.flush()
            session.add(StudyRevisionModel(
                upload_item_id=kind, lecture_id=lecture_id, kind=kind, source_sha256=digest,
                immutable_source_path=str(path), derived_sha256=digest,
                immutable_derived_path=str(path), canonical_derived_path=str(path),
                state="current", current=True,
            ))
    yield StudioRepository(database), IngestionRepository(database)
    database.close()


@pytest.mark.parametrize("connected", [True, False])
def test_new_filed_sources_queue_once_without_backfill_or_remote_effects(filed, connected):
    repository, ingestion = filed
    connection = SimpleNamespace(status=lambda: SimpleNamespace(
        state="connected" if connected else "disconnected"))
    hook = LectureNotebookSync(repository, connection)
    assert repository.list_sources() == []
    for revision in ingestion.list_current_revisions(1):
        hook(revision)
        hook(revision)
    sources = repository.list_sources()
    assert len(sources) == 2
    assert {source.payload_path.suffix for source in sources} == {".pdf", ".txt"}
    for source in sources:
        assert source.snapshot_sha256 == sha256_file(source.payload_path)
        assert source.remote_source_id is None
        assert source.state is (
            StudioSourceState.PENDING if connected else StudioSourceState.NEEDS_REVIEW
        )
        if not connected:
            assert "GPT generation is unaffected" in source.error
    if not connected:
        assert repository.claim_next() is None


def test_upload_identity_conflict_or_stale_revision_is_not_requeued(filed):
    from oms_hub.models import StudioSourceModel

    repository, ingestion = filed
    revision = ingestion.list_current_revisions(1)[0]
    source = repository.enqueue_lecture_upload(revision.id, connected=True)
    with repository.database.session() as session:
        session.get(StudioSourceModel, source.id).snapshot_sha256 = "0" * 64
    with pytest.raises(ValueError, match="already bound"):
        repository.enqueue_lecture_upload(revision.id, connected=True)
    with repository.database.session() as session:
        session.get(StudyRevisionModel, revision.id).current = False
    with pytest.raises(ValueError, match="current filed"):
        repository.enqueue_lecture_upload(revision.id, connected=True)
    assert len(repository.list_sources()) == 1


@pytest.mark.parametrize("changed", [False, True])
def test_filed_upload_uses_existing_durable_lane_and_rechecks_hash(filed, changed):
    repository, ingestion = filed
    revision = ingestion.list_current_revisions(1)[0]
    source = repository.enqueue_lecture_upload(revision.id, connected=True)
    calls = []

    class Gateway:
        def prepare_studio_source_add(self, subject, exam):
            calls.append((subject, exam))
            return "synthetic-notebook", frozenset({"protected-existing-source"})

        def add_studio_source_to_notebook(self, notebook, kind, title, **payload):
            calls.append((notebook, kind))
            assert payload["path"] == revision.immutable_derived_path
            return "new-append-only-source"

    if changed:
        source.payload_path.write_bytes(b"changed")
    worker = StudioWorker(repository, Gateway(), object(), object())
    assert worker.run_once()
    stored = repository.get(source.id)
    if changed:
        assert not calls and stored.state is StudioSourceState.FAILED
        assert "pinned artifact" in stored.error
    else:
        assert len(calls) == 2
        assert stored.state is StudioSourceState.ATTACHED
        assert stored.remote_source_id == "new-append-only-source"
    assert not worker.run_once()


def test_optional_queue_failure_does_not_fail_completed_gpt_ingestion(caplog):
    job = IngestionJob(1, "item", UploadKind.TRANSCRIPTS, "process", 1, datetime.now(UTC),
                       backend="codex_subscription")
    events = []
    result = object()
    repository = SimpleNamespace(claim_next_job=lambda now: job)
    pipeline = SimpleNamespace(process=lambda item: result)

    def stopped_queue(value):
        assert value is result
        events.append("queue")
        raise ValueError("private payload must not be logged")

    worker = IngestionWorker(repository, object(), object(),
                             gpt_transcript_pipeline=pipeline, on_filed=stopped_queue)
    assert worker.run_once() and events == ["queue"]
    assert "Optional NotebookLM queue stopped" in caplog.text
    assert "private payload" not in caplog.text
