from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import event, text

from oms_hub.db import Database
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
from oms_hub.knowledge.normalization import CourseRevisionInput, normalize_course_revision
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.providers.contracts import AuthorityClass


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

    def list_current_revisions(self, lecture_id: int) -> list[StudyRevision]:
        return [
            revision
            for revision in self.revisions.values()
            if revision.lecture_id == lecture_id and revision.current
        ]


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


def test_backfill_maps_current_slide_without_mutating_legacy_revision(
    tmp_path: Path, database: Database
) -> None:
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


def test_backfill_is_idempotent_and_repairs_incomplete_revision(
    tmp_path: Path, database: Database
) -> None:
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
            text(
                "DELETE FROM evidence_units WHERE source_revision_id = :id"
            ),
            {"id": first.revision_id},
        )
    repaired = service.backfill_slide_revision(str(revision.id))
    assert repaired.state is SourceRevisionState.READY
    assert len(knowledge.list_evidence(first.revision_id)) == 1


def test_empty_parser_result_fails_closed_before_any_source_trust_write(
    tmp_path: Path, database: Database
) -> None:
    revision, catalog, _ = _fixture(tmp_path)
    empty = ParsedDocument(
        source_id="legacy-study-revision:7",
        source_sha256=revision.source_sha256,
        source_format="pptx",
        parser_name="fake-legacy",
        parser_version="1",
        segments=(),
        assets=(),
        warnings=(),
    )
    knowledge = KnowledgeRepository(database)
    knowledge.initialize()
    with pytest.raises(ValueError, match="empty evidence"):
        backfill_slide_revision(
            "7",
            ingestion=FakeIngestion({revision.id: revision}),
            catalog=catalog,
            knowledge=knowledge,
            parser=FakeParser(empty),
        )
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM knowledge_sources")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM source_revisions")).scalar_one() == 0


def test_partial_nonempty_evidence_is_repaired_not_already_present(
    tmp_path: Path, database: Database
) -> None:
    revision, catalog, parser = _fixture(tmp_path)
    second = ParsedSegment(
        key="slide-2-block-2",
        kind=SegmentKind.PARAGRAPH,
        text="von Willebrand disease",
        locator=DocumentLocator("slide 2", slide_number=2, block_index=2),
    )
    parser = FakeParser(replace(parser.document, segments=(parser.document.segments[0], second)))
    knowledge = KnowledgeRepository(database)
    knowledge.initialize()
    service = SlideRevisionBackfill(
        FakeIngestion({revision.id: revision}), catalog, knowledge, parser=parser
    )
    candidate = service._prepare("7")
    service.ensure_source_revision(candidate)
    knowledge.put_evidence_units(candidate.source_revision_id, candidate.evidence[:1])

    report = service.backfill_all_ready_course_revisions(1)
    assert report.created == 1
    assert report.already_present == 0
    assert report.failed == 0
    assert len(knowledge.list_evidence(candidate.source_revision_id)) == 2


def test_repeat_parser_normalization_has_stable_locators_and_evidence_ids(
    tmp_path: Path,
) -> None:
    revision, _, parser = _fixture(tmp_path)
    first = normalize_course_revision(
        CourseRevisionInput(
            source_revision_id=make_revision_id("legacy-study-revision:7", revision.source_sha256),
            course_id="course",
            exam_id="exam",
            lecture_id="lecture",
            parsed_document=parser.document,
        )
    )
    second = normalize_course_revision(
        CourseRevisionInput(
            source_revision_id=make_revision_id("legacy-study-revision:7", revision.source_sha256),
            course_id="course",
            exam_id="exam",
            lecture_id="lecture",
            parsed_document=parser.document,
        )
    )
    assert [
        (unit.evidence_id, unit.locator, unit.content_sha256, unit.normalized_text)
        for unit in first
    ] == [
        (unit.evidence_id, unit.locator, unit.content_sha256, unit.normalized_text)
        for unit in second
    ]


def test_batch_is_numeric_limited_continues_after_failure_and_dry_run_writes_nothing(
    tmp_path: Path, database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    valid_dir = tmp_path / "valid"
    invalid_dir = tmp_path / "invalid"
    valid_dir.mkdir()
    invalid_dir.mkdir()
    valid, catalog, parser = _fixture(valid_dir, revision_id=3)
    invalid, _, _ = _fixture(invalid_dir, revision_id=20)
    invalid = replace(invalid, immutable_source_path=invalid_dir / "missing.pptx")
    ingestion = FakeIngestion({20: invalid, 3: valid})
    knowledge = KnowledgeRepository(database)
    knowledge.initialize()
    service = SlideRevisionBackfill(ingestion, catalog, knowledge, parser=parser)
    assert valid.canonical_derived_path is not None
    before_hashes = (
        sha256_file(valid.immutable_source_path),
        sha256_file(valid.canonical_derived_path),
    )

    dry = service.backfill_all_ready_course_revisions(2, dry_run=True)
    assert dry == BackfillReport(2, 1, 0, 1, ("20",))
    assert capsys.readouterr().out == ""
    assert knowledge.get_revision("sr_missing") is None

    report = service.backfill_all_ready_course_revisions(2)
    assert report.examined == 2
    assert report.created == 1
    assert report.already_present == 0
    assert report.failed == 1
    assert report.failure_ids == ("20",)
    limited = service.backfill_all_ready_course_revisions(1)
    assert limited.already_present == 1
    assert before_hashes == (
        sha256_file(valid.immutable_source_path),
        sha256_file(valid.canonical_derived_path),
    )



def test_scope_ids_are_bounded_and_digest_complete() -> None:
    first = scope_ids("A / B", 1, 2)
    second = scope_ids("A:B", 1, 2)
    assert first != second
    assert all(len(value) <= 99 for value in first)
    assert all(
        value.replace("-", "").replace("_", "").replace(".", "").isalnum()
        for value in first
    )
    assert scope_ids("  A / B  ", 1, 2) == scope_ids("a / b", 1, 2)
    with pytest.raises(ValueError):
        scope_ids("", 1, 2)
    with pytest.raises(ValueError):
        scope_ids("course", 0, 2)
    with pytest.raises(ValueError):
        scope_ids("course", 1, 2**63)


def test_backfill_rejects_noncanonical_or_ineligible_revision(
    tmp_path: Path, database: Database
) -> None:
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


@pytest.mark.parametrize("case", ("missing", "catalog", "pdf", "hash", "non-slide"))
def test_ineligible_candidates_fail_without_source_trust_writes(
    tmp_path: Path, database: Database, case: str
) -> None:
    revision, catalog, parser = _fixture(tmp_path)
    if case == "missing":
        ingestion = FakeIngestion({})
    elif case == "catalog":
        ingestion = FakeIngestion({revision.id: revision})
        catalog = FakeCatalog(Lecture(99, "Hematology / Core", 2, 4))
    elif case == "pdf":
        ingestion = FakeIngestion(
            {revision.id: replace(revision, canonical_derived_path=None)}
        )
    elif case == "hash":
        ingestion = FakeIngestion(
            {revision.id: replace(revision, source_sha256="f" * 64)}
        )
    else:
        ingestion = FakeIngestion({revision.id: replace(revision, kind=UploadKind.TRANSCRIPTS)})
    knowledge = KnowledgeRepository(database)
    knowledge.initialize()
    with pytest.raises((KeyError, ValueError)):
        backfill_slide_revision(
            "7",
            ingestion=ingestion,
            catalog=catalog,
            knowledge=knowledge,
            parser=parser,
        )
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM knowledge_sources")).scalar_one() == 0
        assert connection.execute(text("SELECT COUNT(*) FROM source_revisions")).scalar_one() == 0


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


def test_atomic_replacement_is_family_scoped_and_races_independent_engines(
    tmp_path: Path,
) -> None:
    path = tmp_path / "race.db"

    seed = Database(f"sqlite:///{path}")
    try:
        repository = KnowledgeRepository(seed)
        repository.initialize()
        for source_id in (
            "legacy-study-revision:1",
            "legacy-study-revision:2",
            "legacy-study-revision:3",
            "transcript:1",
            "handout:1",
        ):
            repository.create_source(source_id, AuthorityClass.COURSE_MATERIAL)
        predecessor = repository.create_revision(
            "legacy-study-revision:1", "1" * 64, SourceRevisionState.READY
        )
        transcript = repository.create_revision(
            "transcript:1", "4" * 64, SourceRevisionState.READY
        )
        handout = repository.create_revision(
            "handout:1", "5" * 64, SourceRevisionState.READY
        )
        repository.create_revision(
            "legacy-study-revision:2", "2" * 64, SourceRevisionState.NORMALIZING
        )
        repository.create_revision(
            "legacy-study-revision:3", "3" * 64, SourceRevisionState.NORMALIZING
        )
        repository.put_evidence_units(
            predecessor.revision_id,
            (_ready_unit(predecessor.revision_id, source="old", family_text="old"),),
        )
        repository.put_evidence_units(
            transcript.revision_id,
            (
                _ready_unit(
                    transcript.revision_id,
                    source="transcript",
                    family_text="transcript",
                ),
            ),
        )
        repository.put_evidence_units(
            handout.revision_id,
            (
                _ready_unit(
                    handout.revision_id,
                    source="handout",
                    family_text="handout",
                ),
            ),
        )
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
                text(
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
        transcript_state = repository.get_revision(transcript.revision_id)
        predecessor_state = repository.get_revision(predecessor.revision_id)
        assert transcript_state is not None
        assert predecessor_state is not None
        assert transcript_state.state is SourceRevisionState.READY
        handout_state = repository.get_revision(handout.revision_id)
        assert handout_state is not None
        assert handout_state.state is SourceRevisionState.READY
        assert predecessor_state.state is SourceRevisionState.STALE
    finally:
        check.close()


def test_atomic_activation_rolls_back_evidence_and_preserves_predecessor(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'rollback.db'}")
    try:
        repository = KnowledgeRepository(database)
        repository.initialize()
        repository.create_source("legacy-study-revision:10", AuthorityClass.COURSE_MATERIAL)
        repository.create_source("legacy-study-revision:11", AuthorityClass.COURSE_MATERIAL)
        predecessor = repository.create_revision(
            "legacy-study-revision:10", "a" * 64, SourceRevisionState.READY
        )
        replacement = repository.create_revision(
            "legacy-study-revision:11", "b" * 64, SourceRevisionState.NORMALIZING
        )
        previous = _ready_unit(predecessor.revision_id, source="old", family_text="old")
        repository.put_evidence_units(predecessor.revision_id, (previous,))
        invalid = _ready_unit(replacement.revision_id, source="new", family_text="new")
        with pytest.raises(ValueError, match="requested revision scope"):
            repository.activate_revision(
                replacement.revision_id,
                source_family="legacy_slides",
                authority_class=AuthorityClass.COURSE_MATERIAL,
                course_id="different-course",
                exam_id="exam",
                lecture_id="lecture",
                units=(invalid,),
            )
        predecessor_state = repository.get_revision(predecessor.revision_id)
        replacement_state = repository.get_revision(replacement.revision_id)
        assert predecessor_state is not None
        assert replacement_state is not None
        assert predecessor_state.state is SourceRevisionState.READY
        assert replacement_state.state is SourceRevisionState.NORMALIZING
        assert repository.list_evidence(replacement.revision_id) == ()
    finally:
        database.close()


def test_activation_stales_predecessor_before_replacement_is_ready(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'ordering.db'}")
    states: list[str] = []
    try:
        repository = KnowledgeRepository(database)
        repository.initialize()
        repository.create_source("legacy-study-revision:30", AuthorityClass.COURSE_MATERIAL)
        repository.create_source("legacy-study-revision:31", AuthorityClass.COURSE_MATERIAL)
        predecessor = repository.create_revision(
            "legacy-study-revision:30", "a" * 64, SourceRevisionState.READY
        )
        replacement = repository.create_revision(
            "legacy-study-revision:31", "b" * 64, SourceRevisionState.NORMALIZING
        )
        repository.put_evidence_units(
            predecessor.revision_id,
            (_ready_unit(predecessor.revision_id, source="old", family_text="old"),),
        )
        unit = _ready_unit(replacement.revision_id, source="new", family_text="new")

        def observe(
            _connection: object,
            _cursor: object,
            statement: str,
            parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if "UPDATE source_revisions SET state =" not in statement:
                return
            if "SET state = state" in statement:
                return
            if isinstance(parameters, dict):
                states.append(str(parameters["state"]))
            elif isinstance(parameters, (tuple, list)):
                states.append(str(parameters[0]))

        event.listen(database.engine, "after_cursor_execute", observe)
        try:
            repository.activate_revision(
                replacement.revision_id,
                source_family="legacy_slides",
                authority_class=AuthorityClass.COURSE_MATERIAL,
                course_id="course",
                exam_id="exam",
                lecture_id="lecture",
                units=(unit,),
            )
        finally:
            event.remove(database.engine, "after_cursor_execute", observe)
        assert states == ["stale", "ready"]
    finally:
        database.close()


def test_independent_empty_activations_reject_without_staling_predecessor_or_peers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty-race.db"
    seed = Database(f"sqlite:///{path}")
    try:
        repository = KnowledgeRepository(seed)
        repository.initialize()
        for source_id in (
            "legacy-study-revision:40",
            "legacy-study-revision:41",
            "legacy-study-revision:42",
            "transcript:40",
            "handout:40",
        ):
            repository.create_source(source_id, AuthorityClass.COURSE_MATERIAL)
        predecessor = repository.create_revision(
            "legacy-study-revision:40", "a" * 64, SourceRevisionState.READY
        )
        replacement_ids = [
            make_revision_id("legacy-study-revision:41", "b" * 64),
            make_revision_id("legacy-study-revision:42", "c" * 64),
        ]
        replacements = [
            repository.create_revision(
                source_id,
                digest,
                SourceRevisionState.NORMALIZING,
            )
            for source_id, digest in (
                ("legacy-study-revision:41", "b" * 64),
                ("legacy-study-revision:42", "c" * 64),
            )
        ]
        transcript = repository.create_revision(
            "transcript:40", "d" * 64, SourceRevisionState.READY
        )
        handout = repository.create_revision(
            "handout:40", "e" * 64, SourceRevisionState.READY
        )
        repository.put_evidence_units(
            predecessor.revision_id,
            (_ready_unit(predecessor.revision_id, source="old", family_text="old"),),
        )
        repository.put_evidence_units(
            transcript.revision_id,
            (_ready_unit(transcript.revision_id, source="transcript", family_text="transcript"),),
        )
        repository.put_evidence_units(
            handout.revision_id,
            (_ready_unit(handout.revision_id, source="handout", family_text="handout"),),
        )
    finally:
        seed.close()

    barrier = Barrier(2)

    def reject(revision_id: str) -> None:
        database = Database(f"sqlite:///{path}")
        try:
            repository = KnowledgeRepository(database)
            barrier.wait(timeout=5)
            with pytest.raises(ValueError, match="empty evidence"):
                repository.activate_revision(
                    revision_id,
                    source_family="legacy_slides",
                    authority_class=AuthorityClass.COURSE_MATERIAL,
                    course_id="course",
                    exam_id="exam",
                    lecture_id="lecture",
                    units=(),
                )
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reject, revision.revision_id) for revision in replacements]
        for future in futures:
            future.result(timeout=5)

    database = Database(f"sqlite:///{path}")
    try:
        repository = KnowledgeRepository(database)
        assert repository.get_revision(predecessor.revision_id).state is SourceRevisionState.READY
        assert all(
            repository.get_revision(revision_id).state is SourceRevisionState.NORMALIZING
            for revision_id in replacement_ids
        )
        assert repository.get_revision(transcript.revision_id).state is SourceRevisionState.READY
        assert repository.get_revision(handout.revision_id).state is SourceRevisionState.READY
    finally:
        database.close()
