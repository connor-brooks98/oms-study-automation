from __future__ import annotations

import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import IntegrityError

from oms_hub.artifacts.models import ArtifactKind
from oms_hub.artifacts.provenance import ArtifactEvidenceLink, ArtifactRun
from oms_hub.artifacts.repository import ArtifactRepository
from oms_hub.db import Database
from oms_hub.knowledge.ids import sha256_text
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    SourceRevision,
    SourceRevisionState,
)
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.models import (
    GenerationJobModel,
    LectureModel,
    OutlineOutputModel,
    PublishedQuizModel,
    QuizOutputModel,
    StudioRunModel,
    StudyRevisionModel,
    UploadBatchModel,
    UploadItemModel,
)
from oms_hub.providers.contracts import AuthorityClass


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database = Database(f"sqlite:///{tmp_path / 'artifacts.db'}")
    database.create_schema()
    KnowledgeRepository(database).initialize()
    yield database
    database.close()


@pytest.fixture
def repository(database: Database) -> ArtifactRepository:
    repository = ArtifactRepository(database)
    repository.initialize()
    return repository


def _source(
    database: Database,
    *,
    source_document_id: str = "opaque-source-a",
    file_sha256: str = "1" * 64,
    evidence_id: str = "ev-a",
) -> tuple[SourceRevision, EvidenceUnit]:
    knowledge = KnowledgeRepository(database)
    knowledge.create_source(source_document_id, AuthorityClass.COURSE_MATERIAL)
    revision = knowledge.create_revision(
        source_document_id,
        file_sha256,
        SourceRevisionState.READY,
    )
    evidence = EvidenceUnit(
        evidence_id=evidence_id,
        source_revision_id=revision.source_revision_id,
        authority_class=AuthorityClass.COURSE_MATERIAL,
        course_id="course",
        exam_id="exam",
        lecture_id="lecture",
        locator=EvidenceLocator(EvidenceLocatorKind.SLIDE, "1"),
        normalized_text="Synthetic evidence.",
        content_sha256=sha256_text("Synthetic evidence."),
        created_at="2026-08-25T12:00:00+00:00",
    )
    knowledge.put_evidence_units(revision.source_revision_id, (evidence,))
    return revision, evidence


def _run(
    revision: SourceRevision,
    evidence: EvidenceUnit | None = None,
    *,
    artifact_id: str = "artifact-1",
    output_hash: str = "b" * 64,
) -> ArtifactRun:
    return ArtifactRun(
        artifact_id=artifact_id,
        artifact_kind=ArtifactKind.LECTURE_QUIZ,
        recipe_id="lecture-quiz-current",
        recipe_version="current-v1",
        provider="fake",
        model="fake-model",
        prompt_version="prompt-v1",
        schema_version="quiz-v1",
        source_revision_ids=(revision.source_revision_id,),
        evidence_ids=(() if evidence is None else (evidence.evidence_id,)),
        input_hash="a" * 64,
        output_hash=output_hash,
        created_at="2026-08-25T12:00:00+00:00",
        validation_status="valid",
    )


def test_initialize_creates_artifact_provenance_tables(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    del repository
    assert {
        "artifact_runs",
        "artifact_run_sources",
        "artifact_evidence",
    }.issubset(inspect(database.engine).get_table_names())


def test_record_run_is_durable_idempotent_and_immutable(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    revision, evidence = _source(database)
    run = _run(revision, evidence)
    link = ArtifactEvidenceLink(
        artifact_id=run.artifact_id,
        source_revision_id=revision.source_revision_id,
        evidence_id=evidence.evidence_id,
    )

    assert repository.record_run(run, (link,)) == run
    assert repository.record_run(run, (link,)) == run
    assert repository.get_run(run.artifact_id) == run
    assert repository.evidence_links(run.artifact_id) == (link,)

    with pytest.raises(ValueError, match="different provenance"):
        repository.record_run(
            _run(revision, evidence, output_hash="c" * 64),
            (link,),
        )
    assert repository.get_run(run.artifact_id) == run


def test_record_run_validates_source_and_evidence_link_consistency(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    first_revision, _ = _source(database)
    second_revision, second_evidence = _source(
        database,
        source_document_id="opaque-source-b",
        file_sha256="2" * 64,
        evidence_id="ev-b",
    )
    run = _run(first_revision, artifact_id="artifact-mismatch")
    mismatched = ArtifactEvidenceLink(
        artifact_id=run.artifact_id,
        source_revision_id=first_revision.source_revision_id,
        evidence_id=second_evidence.evidence_id,
    )

    with pytest.raises(ValueError, match="evidence.*source revision"):
        repository.record_run(run, (mismatched,))
    assert repository.get_run(run.artifact_id) is None
    assert second_revision.source_revision_id != first_revision.source_revision_id


def test_record_run_rejects_unknown_source_without_partial_write(
    repository: ArtifactRepository,
) -> None:
    missing = SourceRevision(
        source_document_id="missing",
        source_revision_id="sr_missing",
        file_sha256="3" * 64,
        state=SourceRevisionState.READY,
    )
    run = _run(missing, artifact_id="artifact-missing")

    with pytest.raises(KeyError, match="sr_missing"):
        repository.record_run(run)
    assert repository.get_run(run.artifact_id) is None


@pytest.mark.parametrize(
    "state",
    [SourceRevisionState.STALE, SourceRevisionState.RETIRED],
)
def test_record_run_rejects_nonready_source_revision(
    database: Database,
    repository: ArtifactRepository,
    state: SourceRevisionState,
) -> None:
    revision, evidence = _source(database)
    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE source_revisions SET state = :state WHERE id = :id"),
            {"state": state.value, "id": revision.source_revision_id},
        )
    run = _run(revision, evidence, artifact_id=f"artifact-{state.value}")

    with pytest.raises(ValueError, match="ready"):
        repository.record_run(run)
    assert repository.get_run(run.artifact_id) is None


def test_record_run_rejects_retired_evidence(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    revision, evidence = _source(database)
    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE evidence_units SET retired_at = :retired WHERE id = :id"),
            {"retired": "2026-08-25T13:00:00+00:00", "id": evidence.evidence_id},
        )
    run = _run(revision, evidence, artifact_id="artifact-retired-evidence")

    with pytest.raises(ValueError, match="retired evidence"):
        repository.record_run(run)
    assert repository.get_run(run.artifact_id) is None


def test_source_stale_interleaving_cannot_leave_a_nonstale_run(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    revision, evidence = _source(database)
    run = _run(revision, evidence, artifact_id="artifact-interleaving")
    insert_waiting = Event()
    release_insert = Event()
    stale_done = Event()

    def pause_artifact_insert(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().startswith("INSERT INTO artifact_runs"):
            insert_waiting.set()
            assert release_insert.wait(5)

    def stale_revision() -> tuple[str, ...]:
        try:
            with database.engine.begin() as connection:
                connection.execute(
                    text("UPDATE source_revisions SET state = 'stale' WHERE id = :id"),
                    {"id": revision.source_revision_id},
                )
            return ArtifactRepository(database).mark_stale_by_revision(
                revision.source_revision_id
            )
        finally:
            stale_done.set()

    event.listen(database.engine, "before_cursor_execute", pause_artifact_insert)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            recorded = pool.submit(repository.record_run, run)
            assert insert_waiting.wait(5)
            staled = pool.submit(stale_revision)
            stale_done.wait(0.25)
            release_insert.set()
            assert recorded.result(timeout=5).artifact_id == run.artifact_id
            assert staled.result(timeout=5) == (run.artifact_id,)
    finally:
        release_insert.set()
        event.remove(database.engine, "before_cursor_execute", pause_artifact_insert)

    stored = repository.get_run(run.artifact_id)
    assert stored is not None
    assert stored.stale_reason == f"source_revision_stale:{revision.source_revision_id}"


def test_concurrent_exact_record_run_is_idempotent(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    revision, evidence = _source(database)
    run = _run(revision, evidence, artifact_id="artifact-concurrent")
    start = Barrier(2)
    inserts = Barrier(2)

    def synchronize_inserts(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().startswith("INSERT INTO artifact_runs"):
            try:
                inserts.wait(timeout=0.75)
            except BrokenBarrierError:
                pass

    def record() -> ArtifactRun:
        start.wait()
        return ArtifactRepository(database).record_run(run)

    event.listen(database.engine, "before_cursor_execute", synchronize_inserts)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (pool.submit(record), pool.submit(record))
            results = tuple(future.result(timeout=5) for future in futures)
    finally:
        event.remove(database.engine, "before_cursor_execute", synchronize_inserts)

    assert results == (run, run)
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM artifact_runs")).scalar_one() == 1


def test_database_rejects_evidence_source_pair_mismatch(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    first, evidence = _source(database)
    second, _ = _source(
        database,
        source_document_id="opaque-source-b",
        file_sha256="2" * 64,
        evidence_id="ev-b",
    )
    run = replace(
        _run(first, evidence, artifact_id="artifact-pair-write"),
        source_revision_ids=(first.source_revision_id, second.source_revision_id),
    )
    repository.record_run(run)

    with pytest.raises(IntegrityError, match="artifact evidence source mismatch"):
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE artifact_evidence SET source_revision_id = :source "
                    "WHERE artifact_id = :artifact"
                ),
                {"source": second.source_revision_id, "artifact": run.artifact_id},
            )


def test_corrupt_evidence_source_pair_fails_closed_on_read(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    first, evidence = _source(database)
    second, _ = _source(
        database,
        source_document_id="opaque-source-b",
        file_sha256="2" * 64,
        evidence_id="ev-b",
    )
    run = replace(
        _run(first, evidence, artifact_id="artifact-pair-read"),
        source_revision_ids=(first.source_revision_id, second.source_revision_id),
    )
    repository.record_run(run)
    with database.engine.begin() as connection:
        connection.execute(text("DROP TRIGGER IF EXISTS artifact_evidence_source_update"))
        connection.execute(
            text(
                "UPDATE artifact_evidence SET source_revision_id = :source "
                "WHERE artifact_id = :artifact"
            ),
            {"source": second.source_revision_id, "artifact": run.artifact_id},
        )

    with pytest.raises(ValueError, match="stored evidence.*source revision"):
        repository.evidence_links(run.artifact_id)
    with pytest.raises(ValueError, match="stored evidence.*source revision"):
        repository.get_run(run.artifact_id)


def test_mark_stale_by_revision_preserves_dependent_artifacts(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    revision, evidence = _source(database)
    other_revision, _ = _source(
        database,
        source_document_id="opaque-source-b",
        file_sha256="2" * 64,
        evidence_id="ev-b",
    )
    repository.record_run(_run(revision, evidence, artifact_id="artifact-b"))
    repository.record_run(_run(revision, evidence, artifact_id="artifact-a"))
    repository.record_run(_run(other_revision, artifact_id="artifact-other"))

    assert repository.mark_stale_by_revision(revision.source_revision_id) == (
        "artifact-a",
        "artifact-b",
    )
    assert repository.mark_stale_by_revision(revision.source_revision_id) == (
        "artifact-a",
        "artifact-b",
    )
    stale = repository.get_run("artifact-a")
    other = repository.get_run("artifact-other")
    assert stale is not None
    assert other is not None
    assert stale.stale_reason == (
        f"source_revision_stale:{revision.source_revision_id}"
    )
    assert other.stale_reason is None
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM artifact_runs")).scalar_one() == 3


def test_backfill_legacy_outline_and_quiz_without_reconstructing_source_identity(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    source_hash = "4" * 64
    _seed_legacy_generation(database, source_hash)
    _source(
        database,
        source_document_id="legacy-study-revision:7",
        file_sha256=source_hash,
        evidence_id="ev-legacy",
    )

    first = repository.backfill_legacy_artifacts()
    second = repository.backfill_legacy_artifacts()

    assert second == first
    assert [(run.artifact_id, run.recipe_id) for run in first] == [
        ("legacy-outline:11", "lecture-outline-current"),
        ("legacy-lecture-quiz:12", "lecture-quiz-current"),
        ("legacy-custom-quiz:custom-token:v1", "custom-quiz-current"),
    ]
    assert all(run.validation_status == "legacy_unverified" for run in first)
    assert all(run.source_revision_ids == () for run in first)
    assert all(run.evidence_ids == () for run in first)
    custom = first[2]
    assert custom.artifact_kind is ArtifactKind.CUSTOM_QUIZ
    assert custom.output_hash == hashlib.sha256(
        b'{"questions":[],"title":"Synthetic custom quiz"}'
    ).hexdigest()
    assert sum(run.recipe_id == "lecture-quiz-current" for run in first) == 1
    assert sum(run.recipe_id == "custom-quiz-current" for run in first) == 1
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM artifact_evidence")).scalar_one() == 0


def test_custom_backfill_preserves_superseded_publication_versions(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    _seed_legacy_generation(database, "5" * 64)
    first = repository.backfill_legacy_artifacts()[2]

    with database.session() as session:
        session.add(
            _studio_run(
                "studio-run-v2",
                prompt="synthetic prompt v2",
                timestamp="2026-08-25T14:00:00+00:00",
            )
        )
        publication = session.get(PublishedQuizModel, "custom-token")
        assert publication is not None
        publication.studio_run_id = "studio-run-v2"
        publication.payload_json = (
            '{"questions":[],"title":"Synthetic custom quiz v2"}'
        )
        publication.version = 2
        publication.updated_at = "2026-08-25T14:00:00+00:00"

    second = repository.backfill_legacy_artifacts()[2]

    assert first.artifact_id == "legacy-custom-quiz:custom-token:v1"
    assert first.created_at == "2026-08-25T12:00:00+00:00"
    assert second.artifact_id == "legacy-custom-quiz:custom-token:v2"
    assert second.created_at == "2026-08-25T14:00:00+00:00"
    assert first.input_hash != second.input_hash
    assert first.output_hash != second.output_hash
    assert repository.get_run(first.artifact_id) == first
    assert repository.get_run(second.artifact_id) == second


def test_custom_backfill_excludes_direct_import_publications(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    _seed_legacy_generation(database, "5" * 64)
    with database.session() as session:
        session.add(
            _studio_run(
                "direct-import-run",
                workflow_kind="direct_import",
                prompt="synthetic imported payload",
                timestamp="2026-08-25T15:00:00+00:00",
            )
        )
        session.flush()
        session.add(
            _custom_publication(
                "direct-token",
                "direct-import-run",
                '{"questions":[],"title":"Synthetic import"}',
                timestamp="2026-08-25T15:00:00+00:00",
            )
        )

    runs = repository.backfill_legacy_artifacts()

    assert all("direct-token" not in run.artifact_id for run in runs)


def test_backfill_requires_the_exact_cp0002_source_revision_mapping(
    database: Database,
    repository: ArtifactRepository,
) -> None:
    source_hash = "5" * 64
    _seed_legacy_generation(database, source_hash)
    _source(
        database,
        source_document_id="opaque-unrelated-source",
        file_sha256=source_hash,
        evidence_id="ev-unrelated",
    )
    _source(
        database,
        source_document_id="legacy-study-revision:7",
        file_sha256="6" * 64,
        evidence_id="ev-wrong-revision",
    )

    runs = repository.backfill_legacy_artifacts()

    assert all(run.source_revision_ids == () for run in runs)


def _seed_legacy_generation(database: Database, source_hash: str) -> None:
    with database.session() as session:
        session.add(
            LectureModel(
                id=1,
                subject="Synthetic Course",
                exam_number=1,
                lecture_number=1,
                topic="Synthetic topic",
            )
        )
        session.add(UploadBatchModel(id="batch", kind="slides", state="complete"))
        session.flush()
        session.add(
            UploadItemModel(
                id="upload",
                batch_id="batch",
                kind="slides",
                original_filename="synthetic.pptx",
                staged_path="/synthetic/staged.pptx",
                sha256=source_hash,
                size_bytes=1,
                state="complete",
                lecture_id=1,
            )
        )
        session.flush()
        session.add(
            StudyRevisionModel(
                id=7,
                upload_item_id="upload",
                lecture_id=1,
                kind="slides",
                source_sha256=source_hash,
                immutable_source_path="/synthetic/source.pptx",
                state="current",
                current=True,
            )
        )
        session.flush()
        for job_id, kind in (("outline-job", "outline"), ("quiz-job", "quiz")):
            session.add(
                GenerationJobModel(
                    id=job_id,
                    lecture_id=1,
                    kind=kind,
                    state="complete",
                    stage="complete",
                    prompt_sha256="6" * 64,
                    pdf_revision_id=7,
                )
            )
        session.flush()
        session.add(
            OutlineOutputModel(
                id=11,
                lecture_id=1,
                job_id="outline-job",
                path="/synthetic/outline.pdf",
                sha256="7" * 64,
                current=True,
                created_at="2026-08-25T12:00:00+00:00",
            )
        )
        session.add(
            QuizOutputModel(
                id=12,
                lecture_id=1,
                job_id="quiz-job",
                url="/quizzes/synthetic",
                current=True,
                created_at="2026-08-25T12:00:00+00:00",
            )
        )
        session.add(
            PublishedQuizModel(
                token="synthetic-token",
                lecture_id=1,
                job_id="quiz-job",
                title="Synthetic quiz",
                payload_json='{"questions":[],"title":"Synthetic quiz"}',
                created_at="2026-08-25T12:00:00+00:00",
            )
        )
        session.add(_studio_run("studio-run"))
        session.flush()
        session.add(
            _custom_publication(
                "custom-token",
                "studio-run",
                '{"questions":[],"title":"Synthetic custom quiz"}',
            )
        )


def _studio_run(
    run_id: str,
    *,
    workflow_kind: str = "notebook_generation",
    prompt: str = "synthetic prompt",
    timestamp: str = "2026-08-25T12:00:00+00:00",
) -> StudioRunModel:
    return StudioRunModel(
        id=run_id,
        subject="Synthetic Course",
        subject_key="synthetic-course",
        exam_number=1,
        destination_subject="Synthetic Course",
        destination_subject_key="synthetic-course",
        destination_exam_number=1,
        label="Synthetic custom quiz",
        label_key="synthetic-custom-quiz",
        prompt=prompt,
        workflow_kind=workflow_kind,
        content_kind="exam_review",
        state="complete",
        stage="complete",
        created_at=timestamp,
        updated_at=timestamp,
    )


def _custom_publication(
    token: str,
    run_id: str,
    payload_json: str,
    *,
    timestamp: str = "2026-08-25T12:00:00+00:00",
) -> PublishedQuizModel:
    return PublishedQuizModel(
        token=token,
        lecture_id=None,
        job_id=None,
        studio_run_id=run_id,
        destination_subject="Synthetic Course",
        destination_subject_key="synthetic-course",
        destination_exam_number=1,
        label="Synthetic custom quiz",
        label_key="synthetic-custom-quiz",
        title="Synthetic custom quiz",
        payload_json=payload_json,
        content_kind="exam_review",
        version=1,
        created_at=timestamp,
        updated_at=timestamp,
    )
