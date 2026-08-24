from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from oms_hub.db import Database
from oms_hub.indexing.models import IndexState, ProviderStore, StoreKey
from oms_hub.indexing.repository import IndexRepository
from oms_hub.providers.gemini.client import GeminiClientFactory
from oms_hub.providers.gemini.errors import (
    GeminiContractError,
    GeminiProviderError,
    GeminiTransientError,
)
from oms_hub.providers.gemini.file_search import GeminiFileSearchAdmin
from oms_hub.providers.gemini.models import GeminiConfig


class FakeDocuments:
    def __init__(self) -> None:
        self.items: list[object] = []
        self.list_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.delete_error: BaseException | None = None

    async def list(self, **kwargs: object) -> list[object]:
        self.list_calls.append(kwargs)
        return list(self.items)

    async def delete(self, **kwargs: object) -> None:
        self.delete_calls.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error


class FakeStores:
    def __init__(self) -> None:
        self.documents = FakeDocuments()
        self.remote: dict[str, object] = {}
        self.get_calls: list[dict[str, object]] = []
        self.create_calls: list[dict[str, object]] = []
        self.get_error: BaseException | None = None
        self.create_error: BaseException | None = None
        self.next_name = "fileSearchStores/provider-1"
        self.create_started: asyncio.Event | None = None
        self.allow_create: asyncio.Event | None = None

    async def get(self, **kwargs: object) -> object:
        self.get_calls.append(kwargs)
        if self.get_error is not None:
            raise self.get_error
        name = kwargs["name"]
        if name not in self.remote:
            raise GeminiProviderError("remote store missing", provider_status_code=404)
        return self.remote[name]

    async def create(self, **kwargs: object) -> object:
        self.create_calls.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        if self.create_started is not None:
            self.create_started.set()
        if self.allow_create is not None:
            await self.allow_create.wait()
        name = self.next_name
        self.next_name = "fileSearchStores/provider-2"
        created = SimpleNamespace(name=name)
        self.remote[name] = created
        return created


class FakeAioClient:
    def __init__(self, stores: FakeStores) -> None:
        self.file_search_stores = stores
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class FakeSdkClient:
    def __init__(self, aio: FakeAioClient) -> None:
        self.aio = aio


class FakeSdkFactory:
    def __init__(self, client: FakeAioClient) -> None:
        self.client = client
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> FakeSdkClient:
        self.calls.append(kwargs)
        return FakeSdkClient(self.client)


class RawProviderFailure(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.fixture
def database() -> Database:
    value = Database("sqlite://")
    value.create_schema()
    return value


@pytest.fixture
def stores() -> FakeStores:
    return FakeStores()


@pytest.fixture
def admin_bundle(
    database: Database,
    stores: FakeStores,
) -> tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory]:
    client = FakeAioClient(stores)
    sdk = FakeSdkFactory(client)
    factory = GeminiClientFactory(
        GeminiConfig(api_key=SecretStr("provider-secret")),
        sdk_factory=sdk,
    )
    admin = GeminiFileSearchAdmin(database, factory)
    return admin, stores, client, sdk


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def test_ensure_store_creates_remote_store_and_persists_opaque_identity(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
) -> None:
    admin, stores, client, sdk = admin_bundle
    key = StoreKey.course("heme:lymph".replace(":", "-"), "exam-2")

    stored = run(admin.ensure_store(key))

    assert stored.store_key == key
    assert stored.provider_store_name == "fileSearchStores/provider-1"
    assert stored.state is IndexState.READY
    assert stored.provider_store_name != key.display_name
    assert stores.create_calls == [
        {
            "config": {
                "display_name": key.display_name,
                "embedding_model": "models/gemini-embedding-2",
            }
        }
    ]
    assert len(sdk.calls) == 1
    assert client.close_calls == 1
    assert "provider-secret" not in repr(stores.create_calls)


def test_remote_present_ensure_is_idempotent_and_uses_one_client_context(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
) -> None:
    admin, stores, client, sdk = admin_bundle
    key = StoreKey.course("heme-lymph", "exam-2")
    first = run(admin.ensure_store(key))
    stores.create_calls.clear()
    sdk.calls.clear()
    client.close_calls = 0

    second = run(admin.ensure_store(key))

    assert second == first
    assert stores.create_calls == []
    assert len(stores.get_calls) == 1
    assert len(sdk.calls) == 1
    assert client.close_calls == 1


def test_concurrent_ensure_store_creates_once_and_returns_same_identity(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
) -> None:
    admin, stores, _, _ = admin_bundle
    key = StoreKey.course("heme-lymph", "exam-2")

    async def exercise() -> tuple[ProviderStore, ProviderStore]:
        stores.create_started = asyncio.Event()
        stores.allow_create = asyncio.Event()
        first_task = asyncio.create_task(admin.ensure_store(key))
        await stores.create_started.wait()
        second_task = asyncio.create_task(admin.ensure_store(key))
        await asyncio.sleep(0)
        stores.allow_create.set()
        return await asyncio.gather(first_task, second_task)

    first, second = run(exercise())

    assert first.provider_store_name == second.provider_store_name
    assert len(stores.create_calls) == 1


def test_remote_404_commits_orphan_stale_before_replacement_and_changes_identity(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
    database: Database,
) -> None:
    admin, stores, _, _ = admin_bundle
    key = StoreKey.course("heme-lymph", "exam-2")
    first = run(admin.ensure_store(key))
    stores.remote.pop(first.provider_store_name)

    replacement = run(admin.ensure_store(key))
    history = IndexRepository(database).list_store_generations(key)

    assert replacement.generation == 2
    assert replacement.provider_store_name == "fileSearchStores/provider-2"
    assert replacement.provider_store_name != first.provider_store_name
    assert [(item.state, item.is_current) for item in history] == [
        (IndexState.STALE, False),
        (IndexState.READY, True),
    ]
    assert len(stores.create_calls) == 2


def test_failed_replacement_leaves_only_committed_stale_state(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
    database: Database,
) -> None:
    admin, stores, _, _ = admin_bundle
    key = StoreKey.course("heme-lymph", "exam-2")
    first = run(admin.ensure_store(key))
    stores.remote.pop(first.provider_store_name)
    stores.create_error = GeminiTransientError("safe transient failure")

    with pytest.raises(GeminiTransientError):
        run(admin.ensure_store(key))

    repository = IndexRepository(database)
    history = repository.list_store_generations(key)
    assert len(history) == 1
    assert history[0].state is IndexState.STALE
    assert not history[0].is_current


def test_non_404_provider_failure_does_not_mutate_local_store(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
    database: Database,
) -> None:
    admin, stores, _, _ = admin_bundle
    key = StoreKey.course("heme-lymph", "exam-2")
    first = run(admin.ensure_store(key))
    stores.get_error = RawProviderFailure("raw provider secret", 503)

    with pytest.raises(GeminiTransientError) as raised:
        run(admin.ensure_store(key))

    assert raised.value.provider_status_code == 503
    assert "raw provider secret" not in str(raised.value)
    current = IndexRepository(database).get_current_store(key)
    assert current is not None
    assert current == first
    assert current.state is IndexState.READY


def test_repeated_ensure_after_replacement_returns_replacement_without_create(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
) -> None:
    admin, stores, _, _ = admin_bundle
    key = StoreKey.course("heme-lymph", "exam-2")
    first = run(admin.ensure_store(key))
    stores.remote.pop(first.provider_store_name)
    replacement = run(admin.ensure_store(key))
    stores.create_calls.clear()

    repeated = run(admin.ensure_store(key))

    assert repeated == replacement
    assert stores.create_calls == []


def test_list_documents_maps_and_persists_deterministically(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
    database: Database,
) -> None:
    admin, stores, _, _ = admin_bundle
    key = StoreKey.course("heme-lymph", "exam-2")
    stored = run(admin.ensure_store(key))
    stores.documents.items = [
        SimpleNamespace(
            name="fileSearchStores/provider-1/documents/b",
            display_name="B",
            file_name="files/b",
            operation_name="operations/b",
            custom_metadata={"source_revision_id": "sr_b", "lecture_id": "l2"},
            size_bytes=20,
        ),
        SimpleNamespace(
            name="fileSearchStores/provider-1/documents/a",
            display_name="A",
            file_name="files/a",
            operation_name="operations/a",
            custom_metadata={"source_revision_id": "sr_a", "lecture_id": "l1"},
            size_bytes=10,
        ),
    ]

    listed = run(admin.list_documents(stored))

    assert tuple(item.provider_document_id for item in listed) == (
        "fileSearchStores/provider-1/documents/a",
        "fileSearchStores/provider-1/documents/b",
    )
    assert stores.documents.list_calls == [{"parent": stored.provider_store_name}]
    assert IndexRepository(database).get_document(listed[0].id) == listed[0]
    assert listed[0].metadata["source_revision_id"] == "sr_a"


def test_list_documents_accepts_arbitrary_explicit_source_revision_id(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
) -> None:
    admin, stores, _, _ = admin_bundle
    stored = run(admin.ensure_store(StoreKey.course("heme-lymph", "exam-2")))
    stores.documents.items = [
        SimpleNamespace(
            name="fileSearchStores/provider-1/documents/revision",
            custom_metadata={"source_revision_id": "revision"},
        )
    ]

    listed = run(admin.list_documents(stored))

    assert listed[0].source_revision_id == "revision"


@pytest.mark.parametrize(
    "metadata",
    (
        {},
        {"source_revision_id": None},
        {"source_revision_id": " "},
        {"source_revision_id": "revision\x00"},
        {"source_revision_id": "sr_\ninvalid"},
        {"source_revision_id": "sr_" + ("x" * 198)},
    ),
)
def test_list_documents_requires_explicit_bounded_source_revision_id(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
    database: Database,
    metadata: dict[str, object],
) -> None:
    admin, stores, _, _ = admin_bundle
    stored = run(admin.ensure_store(StoreKey.course("heme-lymph", "exam-2")))
    stores.documents.items = [
        SimpleNamespace(
            name="fileSearchStores/provider-1/documents/private-provider-id",
            custom_metadata=metadata,
        )
    ]

    with pytest.raises(GeminiContractError) as raised:
        run(admin.list_documents(stored))

    assert "private-provider-id" not in str(raised.value)
    assert IndexRepository(database).list_documents(stored) == []


def test_invalid_document_metadata_cannot_partially_persist_a_mixed_list(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
    database: Database,
) -> None:
    admin, stores, _, _ = admin_bundle
    stored = run(admin.ensure_store(StoreKey.course("heme-lymph", "exam-2")))
    stores.documents.items = [
        SimpleNamespace(
            name="fileSearchStores/provider-1/documents/a",
            custom_metadata={"source_revision_id": "sr-valid"},
        ),
        SimpleNamespace(
            name="fileSearchStores/provider-1/documents/b",
            custom_metadata={"source_revision_id": "revision\x00"},
        ),
    ]

    with pytest.raises(GeminiContractError):
        run(admin.list_documents(stored))

    assert IndexRepository(database).list_documents(stored) == []


def test_delete_document_marks_deleting_then_deleted_only_after_remote_success(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
    database: Database,
) -> None:
    admin, stores, client, sdk = admin_bundle
    stored = run(admin.ensure_store(StoreKey.course("heme-lymph", "exam-2")))
    stores.documents.items = [
        SimpleNamespace(
            name="fileSearchStores/provider-1/documents/a",
            display_name="A",
            file_name="files/a",
            custom_metadata={"source_revision_id": "sr_a"},
            size_bytes=10,
        )
    ]
    listed = run(admin.list_documents(stored))
    sdk.calls.clear()
    client.close_calls = 0

    run(admin.delete_document(listed[0].provider_document_id))

    current = IndexRepository(database).get_document(listed[0].id)
    assert current is not None
    assert current.state is IndexState.DELETED
    assert stores.documents.delete_calls == [{"name": listed[0].provider_document_id}]
    assert len(sdk.calls) == 1
    assert client.close_calls == 1


def test_failed_remote_delete_never_claims_deleted(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
    database: Database,
) -> None:
    admin, stores, _, _ = admin_bundle
    stored = run(admin.ensure_store(StoreKey.course("heme-lymph", "exam-2")))
    stores.documents.items = [
        SimpleNamespace(
            name="fileSearchStores/provider-1/documents/a",
            display_name="A",
            file_name="files/a",
            custom_metadata={"source_revision_id": "sr_a"},
            size_bytes=10,
        )
    ]
    listed = run(admin.list_documents(stored))
    stores.documents.delete_error = RawProviderFailure("raw delete secret", 503)

    with pytest.raises(GeminiTransientError) as raised:
        run(admin.delete_document(listed[0].provider_document_id))

    assert "raw delete secret" not in str(raised.value)
    current = IndexRepository(database).get_document(listed[0].id)
    assert current is not None
    assert current.state is IndexState.DELETING


def test_translated_404_delete_converges_deleting_document_to_deleted(
    admin_bundle: tuple[GeminiFileSearchAdmin, FakeStores, FakeAioClient, FakeSdkFactory],
    database: Database,
) -> None:
    admin, stores, _, _ = admin_bundle
    stored = run(admin.ensure_store(StoreKey.course("heme-lymph", "exam-2")))
    stores.documents.items = [
        SimpleNamespace(
            name="fileSearchStores/provider-1/documents/a",
            custom_metadata={"source_revision_id": "revision"},
        )
    ]
    listed = run(admin.list_documents(stored))
    stores.documents.delete_error = RawProviderFailure("raw already absent", 404)

    run(admin.delete_document(listed[0].provider_document_id))

    current = IndexRepository(database).get_document(listed[0].id)
    assert current is not None
    assert current.state is IndexState.DELETED
