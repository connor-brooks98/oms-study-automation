from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

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
    ]
    assert all(run.validation_status == "legacy_unverified" for run in first)
    assert all(run.source_revision_ids == () for run in first)
    assert all(run.evidence_ids == () for run in first)
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM artifact_evidence")).scalar_one() == 0


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
