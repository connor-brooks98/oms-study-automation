from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator

import pytest

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
)
from oms_hub.files.atomic import sha256_file
from oms_hub.ingestion.domain import StudyRevision, UploadKind
from oms_hub.knowledge.backfill import (
    BackfillReport,
    SlideRevisionBackfill,
    backfill_all_ready_course_revisions,
    backfill_slide_revision,
    scope_ids,
)
from oms_hub.knowledge.ids import sha256_text
from oms_hub.knowledge.ids import source_revision_id as make_revision_id
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    SourceRevisionState,
)
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.providers.contracts import AuthorityClass
from oms_hub.db import Database


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database = Database(f"sqlite:///{tmp_path / 'knowledge.db'}")
    yield database
    database.close()


@dataclass(frozen=True)
class Lecture:
    id: int
    subject: str
    exam_number: int
    lecture_number: int


class FakeCatalog:
    def __init__(self, lecture: Lecture) -> None:
        self.lecture = lecture

    def get_lecture(self, lecture_id: int) -> Lecture | None:
        return self.lecture if lecture_id == self.lecture.id else None

    def list_lectures(self) -> list[Lecture]:
        return [self.lecture]


class FakeIngestion:
    def __init__(self, revisions: dict[int, StudyRevision]) -> None:
        self.revisions = revisions

    def get_study_revision(self, revision_id: int) -> StudyRevision | None:
        return self.revisions.get(revision_id)

    def list_current_revisions(self, lecture_id: int) -> dict[UploadKind, StudyRevision]:
        return {
            revision.kind: revision
            for revision in self.revisions.values()
            if revision.lecture_id == lecture_id and revision.current
        }


class FakeParser:
    name = "fake-legacy"
    version = "1"

    def __init__(self, document: ParsedDocument) -> None:
        self.document = document

    def parse(self, snapshot: object, asset_root: Path) -> ParsedDocument:
        del snapshot, asset_root
        return self.document


def _fixture(tmp_path: Path, revision_id: int = 7) -> tuple[StudyRevision, FakeCatalog, FakeParser]:
    pptx = tmp_path / f"{revision_id}.pptx"
    pptx.write_bytes(f"pptx-{revision_id}".encode())
    pdf = tmp_path / f"{revision_id}.pdf"
    pdf.write_bytes(f"pdf-{revision_id}".encode())
    source_sha256 = sha256_file(pptx)
    document = ParsedDocument(
        source_id=f"legacy-study-revision:{revision_id}",
        source_sha256=source_sha256,
        source_format="pptx",
        parser_name="fake-legacy",
        parser_version="1",
        segments=(
            ParsedSegment(
                key="slide-1-block-1",
                kind=SegmentKind.PARAGRAPH,
                text="Factor VIII deficiency",
                locator=DocumentLocator("slide 1", slide_number=1, block_index=1),
            ),
        ),
        assets=(),
        warnings=(),
    )
    revision = StudyRevision(
        id=revision_id,
        upload_item_id=f"upload-{revision_id}",
        lecture_id=3,
        kind=UploadKind.SLIDES,
        source_sha256=source_sha256,
        immutable_source_path=pptx,
        derived_sha256=sha256_file(pdf),
        immutable_derived_path=pdf,
        canonical_source_path=pptx,
        canonical_derived_path=pdf,
        icloud_path=None,
        prompt_sha256=None,
        state="current",
        current=True,
    )
    return revision, FakeCatalog(Lecture(3, "Hematology / Core", 2, 4)), FakeParser(document)


def test_backfill_maps_current_slide_without_mutating_legacy_revision(tmp_path: Path, database) -> None:
    revision, catalog, parser = _fixture(tmp_path)
    ingestion = FakeIngestion({revision.id: revision})
    knowledge = KnowledgeRepository(database)
    knowledge.initialize()

    before = revision
    result = backfill_slide_revision(
        str(revision.id),
        ingestion=ingestion,
        catalog=catalog,
        knowledge=knowledge,
        parser=parser,
    )

    assert ingestion.revisions[revision.id] == before
    assert result.state is SourceRevisionState.READY
    assert result.source_document_id == "legacy-study-revision:7"
    assert result.file_sha256 == revision.source_sha256
    assert len(knowledge.list_evidence(result.revision_id)) == 1
    assert knowledge.list_evidence(result.revision_id)[0].exam_id is not None


def test_backfill_is_idempotent_and_repairs_incomplete_revision(tmp_path: Path, database) -> None:
    revision, catalog, parser = _fixture(tmp_path)
    ingestion = FakeIngestion({revision.id: revision})
    knowledge = KnowledgeRepository(database)
    knowledge.initialize()

    service = SlideRevisionBackfill(ingestion, catalog, knowledge, parser=parser)
    first = service.backfill_slide_revision(str(revision.id))
    second = service.backfill_slide_revision(str(revision.id))
    assert first == second

    database_row = knowledge.database.engine
    with database_row.begin() as connection:
        connection.execute(
            __import__("sqlalchemy").text(
                "DELETE FROM evidence_units WHERE source_revision_id = :id"
            ),
            {"id": first.revision_id},
        )
    repaired = service.backfill_slide_revision(str(revision.id))
    assert repaired.state is SourceRevisionState.READY
    assert len(knowledge.list_evidence(first.revision_id)) == 1


def test_scope_ids_are_bounded_and_digest_complete() -> None:
    first = scope_ids("A / B", 1, 2)
    second = scope_ids("A:B", 1, 2)
    assert first != second
    assert all(len(value) <= 99 for value in first)
    assert all(value.replace("-", "").replace("_", "").replace(".", "").isalnum() for value in first)


def test_backfill_rejects_noncanonical_or_ineligible_revision(tmp_path: Path, database) -> None:
    revision, catalog, parser = _fixture(tmp_path)
    ingestion = FakeIngestion({revision.id: revision})
    knowledge = KnowledgeRepository(database)
    knowledge.initialize()
    service = SlideRevisionBackfill(ingestion, catalog, knowledge, parser=parser)

    with pytest.raises(ValueError, match="canonical positive"):
        service.backfill_slide_revision("007")
    with pytest.raises(ValueError, match="current"):
        service = SlideRevisionBackfill(
            FakeIngestion({revision.id: replace(revision, current=False)}),
            catalog,
            knowledge,
            parser=parser,
        )
        service.backfill_slide_revision("7")
    assert knowledge.get_revision("sr_missing") is None


def _ready_unit(revision_id: str, *, source: str, family_text: str) -> EvidenceUnit:
    text = family_text
    return EvidenceUnit(
        evidence_id=f"ev-{revision_id}",
        source_revision_id=revision_id,
        authority_class=AuthorityClass.COURSE_MATERIAL,
        course_id="course",
        exam_id="exam",
        lecture_id="lecture",
        locator=EvidenceLocator(EvidenceLocatorKind.SLIDE, source),
        normalized_text=text,
        content_sha256=sha256_text(text),
        created_at="2026-08-24T12:00:00+00:00",
    )


def test_atomic_replacement_is_family_scoped_and_races_independent_engines(tmp_path: Path) -> None:
    path = tmp_path / "race.db"
    from oms_hub.db import Database

    seed = Database(f"sqlite:///{path}")
    try:
        repository = KnowledgeRepository(seed)
        repository.initialize()
        for source_id in (
            "legacy-study-revision:1",
            "legacy-study-revision:2",
            "legacy-study-revision:3",
            "transcript:1",
        ):
            repository.create_source(source_id, AuthorityClass.COURSE_MATERIAL)
        predecessor = repository.create_revision(
            "legacy-study-revision:1", "1" * 64, SourceRevisionState.READY
        )
        transcript = repository.create_revision("transcript:1", "4" * 64, SourceRevisionState.READY)
        repository.create_revision("legacy-study-revision:2", "2" * 64, SourceRevisionState.NORMALIZING)
        repository.create_revision("legacy-study-revision:3", "3" * 64, SourceRevisionState.NORMALIZING)
        repository.put_evidence_units(predecessor.revision_id, (_ready_unit(predecessor.revision_id, source="old", family_text="old"),))
        repository.put_evidence_units(transcript.revision_id, (_ready_unit(transcript.revision_id, source="transcript", family_text="transcript"),))
    finally:
        seed.close()

    barrier = Barrier(2)

    replacement_ids = {
        "a": make_revision_id("legacy-study-revision:2", "2" * 64),
        "b": make_revision_id("legacy-study-revision:3", "3" * 64),
    }

    def activate(revision_id: str, digest: str) -> None:
        db = Database(f"sqlite:///{path}")
        try:
            repo = KnowledgeRepository(db)
            barrier.wait(timeout=5)
            repo.activate_revision(
                revision_id,
                source_family="legacy_slides",
                authority_class=AuthorityClass.COURSE_MATERIAL,
                course_id="course",
                exam_id="exam",
                lecture_id="lecture",
                units=(_ready_unit(revision_id, source=revision_id, family_text=digest),),
            )
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(activate, replacement_ids[digest], digest)
            for digest in ("a", "b")
        ]
        for future in futures:
            future.result(timeout=5)

    check = Database(f"sqlite:///{path}")
    try:
        repository = KnowledgeRepository(check)
        with check.engine.connect() as connection:
            ready_legacy = connection.execute(
                __import__("sqlalchemy").text(
                    """
                    SELECT COUNT(*) FROM source_revisions r
                    JOIN knowledge_sources s ON s.id = r.source_document_id
                    JOIN evidence_units e ON e.source_revision_id = r.id
                    WHERE r.state = 'ready' AND s.id LIKE 'legacy-study-revision:%'
                      AND e.authority_class = 'course_material'
                      AND e.course_id = 'course' AND e.exam_id = 'exam'
                      AND e.lecture_id = 'lecture'
                    """
                )
            ).scalar_one()
        assert ready_legacy == 1
        assert repository.get_revision(transcript.revision_id).state is SourceRevisionState.READY
        assert repository.get_revision(predecessor.revision_id).state is SourceRevisionState.STALE
    finally:
        check.close()
