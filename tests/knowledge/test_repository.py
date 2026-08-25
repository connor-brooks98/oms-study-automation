from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from typing import Any

import pytest
from sqlalchemy import event, inspect, text

from oms_hub.db import Database
from oms_hub.knowledge.ids import sha256_text
from oms_hub.knowledge.ids import source_revision_id as make_source_revision_id
from oms_hub.knowledge.models import (
    EvidenceLocator,
    EvidenceLocatorKind,
    EvidenceUnit,
    KnowledgeSource,
    SourceRevision,
    SourceRevisionState,
)
from oms_hub.knowledge.repository import KnowledgeRepository
from oms_hub.providers.contracts import AuthorityClass


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    database = Database(f"sqlite:///{tmp_path / 'knowledge.db'}")
    yield database
    database.close()


@pytest.fixture
def repository(database: Database) -> KnowledgeRepository:
    repository = KnowledgeRepository(database)
    repository.initialize()
    repository.create_source(
        source_document_id="source-1",
        authority_class=AuthorityClass.COURSE_MATERIAL,
    )
    return repository


def _unit(
    *,
    evidence_id: str = "ev-1",
    source_revision_id: str = make_source_revision_id("source-1", "a" * 64),
    authority_class: AuthorityClass = AuthorityClass.COURSE_MATERIAL,
    course_id: str | None = "heme",
    normalized_text: str = "Factor VIII deficiency.",
    created_at: str = "2026-08-24T12:00:00+00:00",
) -> EvidenceUnit:
    return EvidenceUnit(
        evidence_id=evidence_id,
        source_revision_id=source_revision_id,
        authority_class=authority_class,
        course_id=course_id,
        exam_id="exam-1",
        lecture_id="lecture-1",
        locator=EvidenceLocator(EvidenceLocatorKind.SLIDE, evidence_id),
        normalized_text=normalized_text,
        content_sha256=sha256_text(normalized_text),
        created_at=created_at,
    )


def _revision(
    *,
    source_document_id: str = "source-1",
    source_revision_id: str | None = None,
    file_sha256: str = "a" * 64,
    state: SourceRevisionState = SourceRevisionState.STAGED,
) -> SourceRevision:
    return SourceRevision(
        source_document_id=source_document_id,
        source_revision_id=source_revision_id
        or make_source_revision_id(source_document_id, file_sha256),
        file_sha256=file_sha256,
        state=state,
    )


def test_initialize_creates_source_trust_tables(database: Database) -> None:
    repository = KnowledgeRepository(database)

    repository.initialize()

    assert {
        "knowledge_sources",
        "source_revisions",
        "evidence_units",
    }.issubset(inspect(database.engine).get_table_names())


def test_initialize_is_idempotent(database: Database) -> None:
    repository = KnowledgeRepository(database)

    repository.initialize()
    repository.initialize()

    assert {
        "knowledge_sources",
        "source_revisions",
        "evidence_units",
    }.issubset(inspect(database.engine).get_table_names())


def test_create_source_round_trips_and_rejects_authority_mismatch(
    repository: KnowledgeRepository,
) -> None:
    source = repository.create_source(
        source_document_id="source-2",
        authority_class=AuthorityClass.PUBLISHED_JOURNAL,
    )

    assert source == KnowledgeSource("source-2", AuthorityClass.PUBLISHED_JOURNAL)
    assert repository.create_source(source) == source
    with pytest.raises(ValueError, match="authority_class"):
        repository.create_source(
            source_document_id="source-2",
            authority_class=AuthorityClass.COURSE_MATERIAL,
        )


def test_create_revision_persists_canonical_id_and_duplicate_hash_returns_existing(
    repository: KnowledgeRepository,
) -> None:
    first = repository.create_revision(
        source_document_id="source-1",
        file_sha256="a" * 64,
        state=SourceRevisionState.STAGED,
    )
    second = repository.create_revision(
        source_document_id="source-1",
        file_sha256="a" * 64,
        state=SourceRevisionState.READY,
    )

    assert first.revision_id == second.revision_id
    assert second.source_revision_id == first.source_revision_id
    assert repository.get_revision(first.revision_id) == first

    with repository.database.engine.connect() as connection:
        row = connection.execute(
            text("SELECT id FROM source_revisions")
        ).fetchone()
    assert row is not None
    assert row[0] == first.source_revision_id


def test_create_revision_accepts_domain_object(repository: KnowledgeRepository) -> None:
    revision = _revision()

    assert repository.create_revision(revision) == revision


def test_create_revision_derives_and_validates_deterministic_identity(
    repository: KnowledgeRepository,
) -> None:
    file_sha256 = "c" * 64
    expected_id = make_source_revision_id("source-1", file_sha256)

    derived = repository.create_revision(
        source_document_id="source-1",
        file_sha256=file_sha256,
        state=SourceRevisionState.STAGED,
    )

    assert derived.source_revision_id == expected_id
    with pytest.raises(ValueError, match="deterministic"):
        repository.create_revision(
            source_document_id="source-1",
            source_revision_id="sr-wrong",
            file_sha256="d" * 64,
            state=SourceRevisionState.STAGED,
        )
    with pytest.raises(ValueError, match="deterministic"):
        repository.create_revision(
            _revision(
                source_revision_id="sr-wrong",
                file_sha256="e" * 64,
            )
        )


def test_create_revision_requires_existing_source(repository: KnowledgeRepository) -> None:
    with pytest.raises(KeyError, match="missing-source"):
        repository.create_revision(
            source_document_id="missing-source",
            file_sha256="a" * 64,
            state=SourceRevisionState.STAGED,
        )


def test_put_and_list_evidence_round_trip_in_deterministic_order(
    repository: KnowledgeRepository,
) -> None:
    revision = repository.create_revision(_revision())
    first = _unit(evidence_id="ev-b", created_at="2026-08-24T12:00:00+00:00")
    second = _unit(evidence_id="ev-a", created_at="2026-08-24T12:00:00+00:00")

    repository.put_evidence_units(revision.revision_id, (first, second))

    assert repository.list_evidence(revision.revision_id) == (second, first)
    assert repository.list_evidence("missing-revision") == ()
    assert repository.dependent_artifact_ids(revision.revision_id) == ()


def test_put_evidence_is_idempotent_for_same_immutable_unit(
    repository: KnowledgeRepository,
) -> None:
    revision = repository.create_revision(_revision())
    unit = _unit()

    repository.put_evidence_units(revision.revision_id, (unit,))
    repository.put_evidence_units(revision.revision_id, (unit,))

    assert repository.list_evidence(revision.revision_id) == (unit,)


def test_put_evidence_rejects_authority_mismatch(repository: KnowledgeRepository) -> None:
    revision = repository.create_revision(_revision())
    unit = _unit(authority_class=AuthorityClass.PUBLISHED_JOURNAL, course_id=None)

    with pytest.raises(ValueError, match="authority_class"):
        repository.put_evidence_units(revision.revision_id, (unit,))
    assert repository.list_evidence(revision.revision_id) == ()


def test_put_evidence_rejects_bad_content_digest(repository: KnowledgeRepository) -> None:
    revision = repository.create_revision(_revision())
    unit = EvidenceUnit(
        evidence_id="ev-bad",
        source_revision_id=revision.revision_id,
        authority_class=AuthorityClass.COURSE_MATERIAL,
        course_id="heme",
        exam_id="exam-1",
        lecture_id="lecture-1",
        locator=EvidenceLocator(EvidenceLocatorKind.SLIDE, "1"),
        normalized_text="Factor VIII deficiency.",
        content_sha256="b" * 64,
    )

    with pytest.raises(ValueError, match="content_sha256"):
        repository.put_evidence_units(revision.revision_id, (unit,))
    assert repository.list_evidence(revision.revision_id) == ()


def test_put_evidence_rejects_unit_for_different_revision(
    repository: KnowledgeRepository,
) -> None:
    first = repository.create_revision(_revision())
    repository.create_revision(
        _revision(
            file_sha256="b" * 64,
        )
    )
    unit = _unit(source_revision_id=make_source_revision_id("source-1", "b" * 64))

    with pytest.raises(ValueError, match="source_revision_id"):
        repository.put_evidence_units(first.revision_id, (unit,))


def test_put_evidence_rolls_back_all_units_on_late_failure(
    repository: KnowledgeRepository,
) -> None:
    revision = repository.create_revision(_revision())
    valid = _unit(evidence_id="ev-valid")
    invalid = _unit(
        evidence_id="ev-invalid",
        normalized_text="Different text",
    )
    object.__setattr__(invalid, "content_sha256", "c" * 64)

    with pytest.raises(ValueError, match="content_sha256"):
        repository.put_evidence_units(revision.revision_id, (valid, invalid))

    assert repository.list_evidence(revision.revision_id) == ()


def test_retire_revision_is_idempotent_and_marks_evidence_retired(
    repository: KnowledgeRepository,
) -> None:
    revision = repository.create_revision(_revision(state=SourceRevisionState.READY))
    unit = _unit()
    repository.put_evidence_units(revision.revision_id, (unit,))

    repository.retire_revision(revision.revision_id)
    retired = repository.get_revision(revision.revision_id)
    retired_unit = repository.list_evidence(revision.revision_id)[0]
    repository.put_evidence_units(revision.revision_id, (retired_unit,))
    repository.retire_revision(revision.revision_id)

    assert retired is not None
    assert retired.state is SourceRevisionState.RETIRED
    retired_evidence = repository.list_evidence(revision.revision_id)
    assert len(retired_evidence) == 1
    assert retired_evidence[0].retired_at is not None
    assert repository.get_revision(revision.revision_id) == retired


def test_retired_revision_rejects_new_evidence(repository: KnowledgeRepository) -> None:
    revision = repository.create_revision(_revision(state=SourceRevisionState.READY))
    repository.retire_revision(revision.revision_id)

    with pytest.raises(ValueError, match="retired"):
        repository.put_evidence_units(
            revision.revision_id,
            (_unit(evidence_id="ev-new"),),
        )

    assert repository.list_evidence(revision.revision_id) == ()


def test_retire_missing_revision_raises_without_writes(
    repository: KnowledgeRepository,
) -> None:
    with pytest.raises(KeyError, match="missing-revision"):
        repository.retire_revision("missing-revision")


def test_schema_has_required_foreign_keys_and_indexes(
    repository: KnowledgeRepository,
) -> None:
    inspector = inspect(repository.database.engine)

    assert inspector.get_pk_constraint("knowledge_sources")["constrained_columns"] == ["id"]
    assert inspector.get_pk_constraint("source_revisions")["constrained_columns"] == ["id"]
    assert inspector.get_pk_constraint("evidence_units")["constrained_columns"] == ["id"]
    assert {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("source_revisions")
    } == {("source_document_id", "file_sha256")}
    assert {
        (
            foreign_key["constrained_columns"][0],
            foreign_key["referred_table"],
            foreign_key["referred_columns"][0],
        )
        for foreign_key in inspector.get_foreign_keys("source_revisions")
    } == {("source_document_id", "knowledge_sources", "id")}
    assert {
        (
            foreign_key["constrained_columns"][0],
            foreign_key["referred_table"],
            foreign_key["referred_columns"][0],
        )
        for foreign_key in inspector.get_foreign_keys("evidence_units")
    } == {("source_revision_id", "source_revisions", "id")}
    assert {
        tuple(index["column_names"])
        for index in inspector.get_indexes("evidence_units")
    } == {
        ("course_id", "exam_id", "lecture_id", "authority_class"),
        ("source_revision_id", "locator_kind", "locator_value"),
    }


def test_dependency_foreign_key_failure_does_not_leave_partial_revision(
    repository: KnowledgeRepository,
) -> None:
    with pytest.raises(KeyError):
        repository.create_revision(
            source_document_id="does-not-exist",
            file_sha256="a" * 64,
            state=SourceRevisionState.STAGED,
        )

    with repository.database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM source_revisions")).scalar_one() == 0


def test_concurrent_source_creation_returns_one_matching_source(
    repository: KnowledgeRepository,
) -> None:
    barrier = Barrier(2)
    pause_lock = Lock()
    pause_count = 0

    def pause_source_lookup(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal pause_count
        if "FROM knowledge_sources" in statement and "WHERE id" in statement:
            with pause_lock:
                if pause_count >= 2:
                    return
                pause_count += 1
            barrier.wait(timeout=5)

    event.listen(repository.database.engine, "before_cursor_execute", pause_source_lookup)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: repository.create_source(
                        source_document_id="source-race",
                        authority_class=AuthorityClass.COURSE_MATERIAL,
                    ),
                    range(2),
                )
            )
    finally:
        event.remove(repository.database.engine, "before_cursor_execute", pause_source_lookup)

    assert results == [results[0], results[0]]


def test_concurrent_revision_creation_returns_one_matching_revision(
    repository: KnowledgeRepository,
) -> None:
    file_sha256 = "f" * 64
    barrier = Barrier(2)
    pause_lock = Lock()
    pause_count = 0

    def pause_revision_lookup(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal pause_count
        if "FROM source_revisions" in statement and "source_document_id" in statement:
            with pause_lock:
                if pause_count >= 2:
                    return
                pause_count += 1
            barrier.wait(timeout=5)

    event.listen(repository.database.engine, "before_cursor_execute", pause_revision_lookup)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: repository.create_revision(
                        source_document_id="source-1",
                        file_sha256=file_sha256,
                        state=SourceRevisionState.STAGED,
                    ),
                    range(2),
                )
            )
    finally:
        event.remove(repository.database.engine, "before_cursor_execute", pause_revision_lookup)

    assert results == [results[0], results[0]]
    assert results[0].source_revision_id == make_source_revision_id("source-1", file_sha256)


def test_concurrent_evidence_retry_keeps_the_whole_batch(
    repository: KnowledgeRepository,
) -> None:
    revision = repository.create_revision(_revision())
    units = (_unit(evidence_id="ev-race-a"), _unit(evidence_id="ev-race-b"))
    barrier = Barrier(2)
    pause_lock = Lock()
    pause_count = 0

    def pause_evidence_lookup(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal pause_count
        if "JOIN knowledge_sources" in statement:
            with pause_lock:
                if pause_count >= 2:
                    return
                pause_count += 1
            barrier.wait(timeout=5)

    event.listen(repository.database.engine, "before_cursor_execute", pause_evidence_lookup)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda _: repository.put_evidence_units(revision.revision_id, units),
                    range(2),
                )
            )
    finally:
        event.remove(repository.database.engine, "before_cursor_execute", pause_evidence_lookup)

    assert results == [None, None]
    assert repository.list_evidence(revision.revision_id) == units


def test_retirement_winning_interleaving_rejects_evidence(
    repository: KnowledgeRepository,
) -> None:
    revision = repository.create_revision(_revision(state=SourceRevisionState.READY))
    unit = _unit(evidence_id="ev-retirement-wins")
    read_complete = Event()
    allow_put = Event()
    first_read = Lock()
    read_blocked = False

    def pause_after_revision_read(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal read_blocked
        if "JOIN knowledge_sources" not in statement:
            return
        with first_read:
            if read_blocked:
                return
            read_blocked = True
        read_complete.set()
        if not allow_put.wait(timeout=5):
            raise TimeoutError("put was not released")

    event.listen(repository.database.engine, "after_cursor_execute", pause_after_revision_read)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            put_future = executor.submit(
                repository.put_evidence_units,
                revision.revision_id,
                (unit,),
            )
            assert read_complete.wait(timeout=5)
            repository.retire_revision(revision.revision_id)
            allow_put.set()
            with pytest.raises(ValueError, match="retired"):
                put_future.result(timeout=5)
    finally:
        event.remove(repository.database.engine, "after_cursor_execute", pause_after_revision_read)

    assert repository.list_evidence(revision.revision_id) == ()


def test_evidence_winning_interleaving_is_stamped_by_retirement(
    repository: KnowledgeRepository,
) -> None:
    revision = repository.create_revision(_revision(state=SourceRevisionState.READY))
    unit = _unit(evidence_id="ev-evidence-wins")
    claim_complete = Event()
    allow_insert = Event()
    retirement_update_started = Event()

    def pause_after_claim(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if "SET state = state" not in statement:
            return
        claim_complete.set()
        if not allow_insert.wait(timeout=5):
            raise TimeoutError("insert was not released")

    def observe_retirement_update(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if (
            "UPDATE source_revisions SET state =" in statement
            and "SET state = state" not in statement
        ):
            retirement_update_started.set()

    event.listen(repository.database.engine, "after_cursor_execute", pause_after_claim)
    event.listen(repository.database.engine, "before_cursor_execute", observe_retirement_update)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            put_future = executor.submit(
                repository.put_evidence_units,
                revision.revision_id,
                (unit,),
            )
            assert claim_complete.wait(timeout=5)
            retire_future = executor.submit(
                repository.retire_revision,
                revision.revision_id,
            )
            assert retirement_update_started.wait(timeout=5)
            allow_insert.set()
            assert put_future.result(timeout=5) is None
            assert retire_future.result(timeout=5) is None
    finally:
        event.remove(repository.database.engine, "after_cursor_execute", pause_after_claim)
        event.remove(
            repository.database.engine,
            "before_cursor_execute",
            observe_retirement_update,
        )

    stored = repository.list_evidence(revision.revision_id)
    assert len(stored) == 1
    assert stored[0].retired_at is not None
