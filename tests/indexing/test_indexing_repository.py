from __future__ import annotations

import itertools
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oms_hub.db import Database
from oms_hub.indexing.models import (
    ALLOWED_TRANSITIONS,
    IndexJob,
    IndexJobModel,
    IndexState,
    ProviderDocument,
    ProviderDocumentModel,
    ProviderStore,
    ProviderStoreModel,
    StoreKey,
    validate_transition,
)
from oms_hub.indexing.repository import IndexRepository


@pytest.fixture
def database() -> Database:
    value = Database("sqlite://")
    value.create_schema()
    return value


def course_key() -> StoreKey:
    return StoreKey.course("heme-lymph", "exam-2")


def literature_key() -> StoreKey:
    return StoreKey.literature("heme-lymph")


def store(
    key: StoreKey | None = None,
    *,
    provider_store_name: str = "fileSearchStores/opaque-1",
    generation: int = 1,
    current: bool = True,
    state: IndexState = IndexState.READY,
) -> ProviderStore:
    resolved_key = key or course_key()
    return ProviderStore(
        store_key=resolved_key,
        provider="gemini",
        provider_store_name=provider_store_name,
        embedding_model="models/gemini-embedding-2",
        authority_namespace=resolved_key.authority_namespace,
        course_id=resolved_key.course_id,
        exam_id=resolved_key.exam_id,
        state=state,
        generation=generation,
        is_current=current,
    )


def document(
    stored: ProviderStore,
    *,
    provider_document_id: str = "fileSearchStores/opaque-1/documents/doc-1",
    source_revision_id: str = "sr_lecture_1",
    state: IndexState = IndexState.READY,
) -> ProviderDocument:
    return ProviderDocument(
        store_id=stored.id,
        provider="gemini",
        provider_document_id=provider_document_id,
        source_revision_id=source_revision_id,
        provider_file_name="files/file-1",
        provider_document_name=provider_document_id,
        provider_operation_name="operations/import-1",
        input_byte_count=1234,
        metadata={"source_revision_id": source_revision_id, "lecture_id": "lecture-1"},
        state=state,
        retry_count=2,
        last_error_category="transient" if state is IndexState.RETRYABLE_FAILURE else None,
    )


def test_every_unlisted_state_transition_is_rejected() -> None:
    states = tuple(IndexState)
    for before, after in itertools.product(states, repeat=2):
        if after in ALLOWED_TRANSITIONS.get(before, frozenset()):
            validate_transition(before, after)
        else:
            with pytest.raises(ValueError):
                validate_transition(before, after)


def test_required_state_transitions_are_allowed() -> None:
    allowed = {
        (IndexState.NOT_INDEXED, IndexState.UPLOADING_FILE),
        (IndexState.UPLOADING_FILE, IndexState.FILE_UPLOADED),
        (IndexState.FILE_UPLOADED, IndexState.IMPORTING),
        (IndexState.IMPORTING, IndexState.READY),
        (IndexState.READY, IndexState.STALE),
        (IndexState.RETRYABLE_FAILURE, IndexState.UPLOADING_FILE),
        (IndexState.RETRYABLE_FAILURE, IndexState.IMPORTING),
        (IndexState.READY, IndexState.DELETING),
        (IndexState.DELETING, IndexState.DELETED),
        (IndexState.STALE, IndexState.UPLOADING_FILE),
        (IndexState.TERMINAL_FAILURE, IndexState.UPLOADING_FILE),
    }
    assert allowed <= {
        (before, after)
        for before, afters in ALLOWED_TRANSITIONS.items()
        for after in afters
    }


@pytest.mark.parametrize(
    ("factory", "value"),
    (
        (StoreKey.course, ("heme-lymph", "exam-2")),
        (StoreKey.literature, ("heme-lymph",)),
    ),
)
def test_store_key_namespaces_are_exact_and_parseable(
    factory: Callable[..., StoreKey],
    value: tuple[str, ...],
) -> None:
    key = factory(*value)
    assert str(key) == (
        "course:heme-lymph:exam:exam-2"
        if key.kind == "course"
        else "literature:heme-lymph"
    )
    assert StoreKey.parse(str(key)) == key
    assert key.display_name
    assert len(key.display_name) <= StoreKey.MAX_DISPLAY_NAME_LENGTH


@pytest.mark.parametrize(
    "builder",
    (
        lambda: StoreKey.course("", "exam-2"),
        lambda: StoreKey.course("heme:lymph", "exam-2"),
        lambda: StoreKey.course("heme-lymph", "exam:2"),
        lambda: StoreKey.literature("heme/lymph"),
        lambda: StoreKey.literature("x" * 101),
    ),
)
def test_store_key_rejects_ambiguous_or_unbounded_identifiers(
    builder: Callable[[], StoreKey],
) -> None:
    with pytest.raises(ValueError):
        builder()


def test_store_key_rejects_malformed_namespace() -> None:
    with pytest.raises(ValueError):
        StoreKey.parse("course:heme-lymph")
    with pytest.raises(ValueError):
        StoreKey.parse("course:heme-lymph:exam:exam-2:extra")


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("course_id", "different-course"),
        ("exam_id", "exam-9"),
        ("authority_namespace", "published_journal"),
    ),
)
def test_provider_store_rejects_scope_fields_mismatched_to_store_key(
    field_name: str,
    value: str,
) -> None:
    key = course_key()
    values: dict[str, Any] = {
        "store_key": key,
        "provider": "gemini",
        "provider_store_name": "fileSearchStores/opaque-1",
        "embedding_model": "models/gemini-embedding-2",
        "authority_namespace": key.authority_namespace,
        "course_id": key.course_id,
        "exam_id": key.exam_id,
    }
    values[field_name] = value

    with pytest.raises(ValueError, match=field_name.replace("_", " ")):
        ProviderStore(**values)


def test_repository_round_trips_store_document_and_job(database: Database) -> None:
    repository = IndexRepository(database)
    saved_store = repository.create_store(store())
    saved_document = repository.save_document(document(saved_store))
    saved_job = repository.save_job(
        IndexJob(
            store_id=saved_store.id,
            source_revision_id="sr_lecture_1",
            provider_document_id=saved_document.provider_document_id,
            provider_operation_name="operations/import-1",
            state=IndexState.IMPORTING,
            retry_count=1,
            last_error_category="quota",
        )
    )

    round_trip_store = repository.get_current_store(saved_store.store_key)
    round_trip_document = repository.get_document(saved_document.id)
    round_trip_job = repository.get_job(saved_job.id)

    assert round_trip_store == saved_store
    assert round_trip_document == saved_document
    assert round_trip_document.metadata == {
        "source_revision_id": "sr_lecture_1",
        "lecture_id": "lecture-1",
    }
    assert round_trip_job == saved_job


def test_store_history_keeps_stale_orphan_and_replacement(database: Database) -> None:
    repository = IndexRepository(database)
    first = repository.create_store(store())
    repository.mark_store_stale(first.id)
    replacement = repository.create_store(
        store(
            provider_store_name="fileSearchStores/opaque-2",
            generation=2,
            current=True,
        )
    )

    assert repository.get_current_store(first.store_key) == replacement
    history = repository.list_store_generations(first.store_key)
    assert [(item.generation, item.provider_store_name, item.state) for item in history] == [
        (1, "fileSearchStores/opaque-1", IndexState.STALE),
        (2, "fileSearchStores/opaque-2", IndexState.READY),
    ]


def test_database_rejects_duplicate_current_store_key(database: Database) -> None:
    repository = IndexRepository(database)
    repository.create_store(store())
    with pytest.raises(IntegrityError):
        repository.create_store(store(provider_store_name="fileSearchStores/opaque-2"))


def test_database_rejects_duplicate_provider_identity_and_document_identity(
    database: Database,
) -> None:
    repository = IndexRepository(database)
    saved_store = repository.create_store(store())
    with pytest.raises(IntegrityError):
        repository.create_store(
            store(
                key=literature_key(),
                provider_store_name=saved_store.provider_store_name,
            )
        )

    saved_document = repository.save_document(document(saved_store))
    with pytest.raises(IntegrityError):
        repository.save_document(document(saved_store))
    with pytest.raises(IntegrityError):
        repository.save_document(
            document(
                saved_store,
                provider_document_id="fileSearchStores/opaque-1/documents/doc-2",
                source_revision_id="sr_lecture_1",
            )
        )
    assert repository.get_document(saved_document.id) is not None


def test_repository_persists_multiple_bounded_inputs_for_one_revision(
    database: Database,
) -> None:
    repository = IndexRepository(database)
    saved_store = repository.create_store(store())
    pdf = repository.save_document(
        ProviderDocument(
            store_id=saved_store.id,
            provider="gemini",
            provider_document_id="documents/pdf",
            source_revision_id="sr_lecture_1",
            input_key="pdf",
            input_kind="pdf",
            input_sha256="a" * 64,
        )
    )
    markdown = repository.save_document(
        ProviderDocument(
            store_id=saved_store.id,
            provider="gemini",
            provider_document_id="documents/markdown",
            source_revision_id="sr_lecture_1",
            input_key="normalized_markdown",
            input_kind="markdown",
            input_sha256="b" * 64,
        )
    )

    assert repository.get_document_by_source_revision(
        saved_store.id, "sr_lecture_1", input_key="pdf"
    ) == pdf
    assert repository.get_document_by_source_revision(
        saved_store.id, "sr_lecture_1", input_key="normalized_markdown"
    ) == markdown
    assert [item.input_key for item in repository.list_documents(saved_store)] == [
        "normalized_markdown",
        "pdf",
    ]


def test_repository_does_not_create_schema_in_constructor(tmp_path: Path) -> None:
    path = tmp_path / "not-created.sqlite"
    database = Database(f"sqlite:///{path}")
    IndexRepository(database)
    assert not path.exists()


def test_raw_rows_are_registered_by_owned_model_import(database: Database) -> None:
    tables = set(database.engine.dialect.get_table_names(database.engine.connect()))
    assert {"provider_stores", "provider_documents", "index_jobs"} <= tables
    with database.session() as session:
        assert session.scalar(select(ProviderStoreModel)) is None
        assert session.scalar(select(ProviderDocumentModel)) is None
        assert session.scalar(select(IndexJobModel)) is None
