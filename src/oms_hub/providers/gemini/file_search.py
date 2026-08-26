"""Local-only Gemini File Search store and document administration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Iterable, Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from time import monotonic
from typing import Any, cast
from weakref import WeakKeyDictionary

from oms_hub.db import Database
from oms_hub.indexing.models import (
    IndexState,
    ProviderDocument,
    ProviderStore,
    StoreKey,
)
from oms_hub.indexing.repository import IndexRepository
from oms_hub.providers.gemini.client import GeminiClientFactory, translate_gemini_error
from oms_hub.providers.gemini.errors import (
    GeminiContractError,
    GeminiProviderError,
    GeminiTransientError,
)


@dataclass(frozen=True, slots=True)
class UploadedFileRef:
    name: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class OperationRef:
    name: str


@dataclass(frozen=True, slots=True)
class CompletedOperation:
    name: str
    document_name: str


@dataclass(frozen=True, slots=True)
class RemoteDocumentObservation:
    provider_document_id: str
    source_revision_id: str | None = None
    input_key: str | None = None
    input_kind: str | None = None
    input_sha256: str | None = None
    input_byte_count: int | None = None
    provider_file_name: str | None = None
    provider_document_name: str | None = None
    provider_operation_name: str | None = None
    metadata_json: str = "{}"
    validation_error: str | None = None


class GeminiFileSearchAdmin:
    """Administer provider identities while keeping lifecycle state local."""

    def __init__(
        self,
        database: Database | IndexRepository,
        client_factory: GeminiClientFactory,
        repository: IndexRepository | None = None,
    ) -> None:
        if repository is not None:
            self.repository = repository
        elif isinstance(database, IndexRepository):
            self.repository = database
        else:
            self.repository = IndexRepository(database)
        self.client_factory = client_factory
        self._ensure_locks: WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Lock
        ] = WeakKeyDictionary()

    async def ensure_store(self, key: StoreKey) -> ProviderStore:
        key = _require_store_key(key)
        async with self._ensure_lock():
            return await self._ensure_store_locked(key)

    async def _ensure_store_locked(self, key: StoreKey) -> ProviderStore:
        current = self.repository.get_current_store(key)
        async with self.client_factory.client() as client:
            stores = _stores_api(client)
            if current is not None:
                try:
                    remote = await _call_provider(
                        stores.get,
                        name=current.provider_store_name,
                    )
                except Exception as error:
                    translated = _translate(error)
                    if translated.provider_status_code != 404:
                        raise translated from None
                    self.repository.mark_store_stale(current.id)
                else:
                    if remote is not None:
                        return current
                    self.repository.mark_store_stale(current.id)

            created = await _call_provider(
                stores.create,
                config=_store_config(key, self.client_factory),
            )
            provider_store_name = _provider_identity(created, "store")
            if current is not None and provider_store_name == current.provider_store_name:
                raise GeminiContractError(
                    "Gemini replacement reused the stale provider store identity."
                )
            replacement = ProviderStore(
                store_key=key,
                provider="gemini",
                provider_store_name=provider_store_name,
                embedding_model=self.client_factory.config.embedding_model,
                authority_namespace=key.authority_namespace,
                course_id=key.course_id,
                exam_id=key.exam_id,
                state=IndexState.READY,
                generation=self.repository.next_store_generation(key),
                is_current=True,
            )
            self.repository.create_store(replacement)
            return replacement

    async def list_documents(self, store: ProviderStore) -> tuple[ProviderDocument, ...]:
        if not isinstance(store, ProviderStore):
            raise TypeError("store must be a persisted ProviderStore")
        async with self.client_factory.client() as client:
            raw_documents = await _call_provider(
                _documents_api(client).list,
                parent=store.provider_store_name,
            )
            entries = await _collect_documents(raw_documents)
        mapped = [_document_from_provider(entry, store) for entry in entries]
        mapped.sort(key=lambda item: (item.provider_document_id, item.id))
        return tuple(self.repository.upsert_document(item) for item in mapped)

    async def snapshot_documents(
        self, store: ProviderStore
    ) -> tuple[RemoteDocumentObservation, ...]:
        if not isinstance(store, ProviderStore):
            raise TypeError("store must be a persisted ProviderStore")
        async with self.client_factory.client() as client:
            documents_api = _documents_api(client)
            raw_documents = await _call_provider(
                documents_api.list,
                parent=store.provider_store_name,
            )
            entries = await _collect_documents(raw_documents)

        mapped = [_observation_from_provider(entry, store) for entry in entries]
        mapped.sort(
            key=lambda item: (
                item.validation_error is not None,
                item.input_key or "",
                item.provider_document_id,
            )
        )
        return tuple(mapped)

    async def upload_file(self, path: Path, display_name: str) -> UploadedFileRef:
        path = Path(path)
        display_name = _required_text(display_name, "file display name")
        if not path.is_file():
            raise GeminiContractError("Gemini upload source is not a regular file.")
        size_bytes = path.stat().st_size
        if size_bytes > self.client_factory.config.maximum_document_bytes:
            raise GeminiContractError("Gemini upload source exceeds the configured size limit.")
        async with self.client_factory.client() as client:
            files = _files_api(client)
            uploaded = await _call_provider(
                files.upload,
                file=path,
                config={"display_name": display_name},
            )
        return UploadedFileRef(_provider_identity(uploaded, "file"), size_bytes)

    async def import_file(
        self,
        store_name: str,
        file_name: str,
        metadata: object,
        chunking: object | None,
    ) -> OperationRef:
        store_name = _required_text(store_name, "store name")
        file_name = _required_text(file_name, "file name")
        config = {"custom_metadata": metadata}
        if chunking is not None:
            config["chunking_config"] = chunking
        async with self.client_factory.client() as client:
            operation = await _call_provider(
                _stores_api(client).import_file,
                file_search_store_name=store_name,
                file_name=file_name,
                config=config,
            )
        return OperationRef(_provider_identity(operation, "operation"))

    async def wait_for_operation(self, operation_name: str) -> CompletedOperation:
        operation_name = _required_text(operation_name, "operation name")
        deadline = monotonic() + self.client_factory.config.operation_timeout_seconds
        operation = _import_operation(operation_name)
        attempt = 0
        async with self.client_factory.client() as client:
            operations = _operations_api(client)
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise GeminiTransientError(
                        "Gemini import operation timed out; resume the persisted operation."
                    )
                try:
                    async with asyncio.timeout(remaining):
                        operation = await _call_provider(operations.get, operation)
                except TimeoutError:
                    raise GeminiTransientError(
                        "Gemini import operation timed out; resume the persisted operation."
                    ) from None
                if bool(_value(operation, "done")):
                    return _completed_operation(operation_name, operation)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise GeminiTransientError(
                        "Gemini import operation timed out; resume the persisted operation."
                    )
                delay = min(
                    self.client_factory.config.operation_poll_seconds * (2**attempt),
                    15,
                    remaining,
                )
                await asyncio.sleep(delay)
                attempt += 1

    async def delete_file(self, file_name: str) -> None:
        file_name = _required_text(file_name, "file name")
        async with self.client_factory.client() as client:
            try:
                await _call_provider(_files_api(client).delete, name=file_name)
            except GeminiProviderError as error:
                if error.provider_status_code != 404:
                    raise

    async def delete_document(self, provider_document_id: str) -> None:
        document = self.repository.get_document_by_provider_id(provider_document_id)
        if document is None:
            raise KeyError(provider_document_id)
        if document.state is IndexState.DELETED:
            return
        if document.state is not IndexState.DELETING:
            document = self.repository.mark_document_deleting(provider_document_id)
        async with self.client_factory.client() as client:
            documents_api = _documents_api(client)
            try:
                delete_method = documents_api.delete
            except Exception as error:
                raise _translate(error) from None
            try:
                await _call_provider(delete_method, name=document.provider_document_id)
            except Exception as error:
                translated = _translate(error)
                if translated.provider_status_code == 404:
                    self.repository.mark_document_deleted(provider_document_id)
                    return
                raise translated from None
        self.repository.mark_document_deleted(provider_document_id)

    async def delete_remote_document(self, provider_document_id: str) -> None:
        provider_document_id = _required_text(provider_document_id, "document name")
        async with self.client_factory.client() as client:
            try:
                await _call_provider(_documents_api(client).delete, name=provider_document_id)
            except GeminiProviderError as error:
                if error.provider_status_code != 404:
                    raise

    def _ensure_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._ensure_locks.get(loop)
        if lock is None:
            # ponytail: one per-admin lock serializes all store keys; upgrade to
            # per-key/distributed coordination only if throughput or multi-process
            # activation requires it.
            lock = asyncio.Lock()
            self._ensure_locks[loop] = lock
        return lock


def _require_store_key(key: StoreKey) -> StoreKey:
    if not isinstance(key, StoreKey):
        raise TypeError("ensure_store requires a StoreKey")
    return key


def _store_config(key: StoreKey, factory: GeminiClientFactory) -> dict[str, object]:
    return {
        "display_name": key.display_name,
        "embedding_model": factory.config.embedding_model,
    }


def _stores_api(client: object) -> Any:
    stores = _safe_attr(client, "file_search_stores")
    if stores is None:
        raise GeminiContractError("Gemini SDK does not expose File Search stores.")
    return stores


def _documents_api(client: object) -> Any:
    stores = _stores_api(client)
    documents = _safe_attr(stores, "documents")
    if documents is None:
        documents = _safe_attr(client, "file_search_documents")
    if documents is None:
        raise GeminiContractError("Gemini SDK does not expose File Search documents.")
    return documents


def _files_api(client: object) -> Any:
    files = _safe_attr(client, "files")
    if files is None:
        raise GeminiContractError("Gemini SDK does not expose Files.")
    return files


def _operations_api(client: object) -> Any:
    operations = _safe_attr(client, "operations")
    if operations is None:
        raise GeminiContractError("Gemini SDK does not expose operations.")
    return operations


async def _call_provider(method: Any, *args: object, **kwargs: object) -> Any:
    try:
        result = method(*args, **kwargs)
        if hasattr(result, "__await__"):
            return await result
        raise GeminiContractError("Gemini File Search admin method was not asynchronous.")
    except GeminiProviderError:
        raise
    except Exception as error:
        raise _translate(error) from None


def _translate(error: Exception) -> GeminiProviderError:
    return error if isinstance(error, GeminiProviderError) else translate_gemini_error(error)


def _provider_identity(value: object, kind: str) -> str:
    candidate = _value(value, "name", "id")
    if not isinstance(candidate, str) or not candidate.strip():
        raise GeminiContractError(f"Gemini {kind} response omitted its provider identity.")
    normalized = candidate.strip()
    if len(normalized) > 500 or not normalized.isprintable():
        raise GeminiContractError(f"Gemini {kind} response contained an invalid provider identity.")
    return normalized


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GeminiContractError(f"Gemini {label} is missing.")
    normalized = value.strip()
    if len(normalized) > 500 or not normalized.isprintable():
        raise GeminiContractError(f"Gemini {label} is invalid.")
    return normalized


def _import_operation(name: str) -> object:
    try:
        operation_type = import_module("google.genai.types").ImportFileOperation
    except (AttributeError, ImportError, ModuleNotFoundError):
        return OperationRef(name)
    try:
        return operation_type(name=name)
    except Exception as error:
        raise _translate(error) from None


def _completed_operation(name: str, operation: object) -> CompletedOperation:
    error = _value(operation, "error")
    if error:
        status = _value(error, "code", "status_code", "status")
        translated = _translate(_OperationFailure(status if isinstance(status, int) else None))
        raise translated from None
    response = _value(operation, "response")
    document_name = _value(response, "document_name", "documentName")
    return CompletedOperation(name, _required_text(document_name, "document name"))


class _OperationFailure(Exception):
    def __init__(self, status_code: int | None) -> None:
        self.status_code = status_code
        super().__init__("Gemini import operation failed.")


async def _collect_documents(value: object) -> list[object]:
    candidate = _value(value, "documents", "items")
    if candidate is not None:
        value = candidate
    if isinstance(value, AsyncIterable):
        return [item async for item in value]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return list(value)
    raise GeminiContractError("Gemini document list response did not contain documents.")


def _document_from_provider(value: object, store: ProviderStore) -> ProviderDocument:
    provider_document_id = _provider_identity(value, "document")
    metadata = _json_metadata(_value(value, "custom_metadata", "customMetadata", "metadata"))
    source_revision_id = _require_source_revision_id(metadata)
    provider_file_name = _optional_text(_value(value, "file_name", "fileName", "file"))
    provider_document_name = _optional_text(
        _value(value, "display_name", "displayName", "document_name", "documentName")
    )
    provider_operation_name = _optional_text(
        _value(value, "operation_name", "operationName", "operation")
    )
    size_value = _value(value, "size_bytes", "sizeBytes", "input_byte_count", "bytes")
    input_byte_count = size_value if isinstance(size_value, int) and size_value >= 0 else None
    return ProviderDocument(
        store_id=store.id,
        provider="gemini",
        provider_document_id=provider_document_id,
        source_revision_id=source_revision_id,
        provider_file_name=provider_file_name,
        provider_document_name=provider_document_name,
        provider_operation_name=provider_operation_name,
        input_byte_count=input_byte_count,
        metadata=metadata,
        state=IndexState.READY,
    )


def _observation_from_provider(
    value: object,
    store: ProviderStore,
) -> RemoteDocumentObservation:
    provider_document_id = _provider_identity(value, "document")
    metadata = _json_metadata(_value(value, "custom_metadata", "customMetadata", "metadata"))
    try:
        source_revision_id = _unique_metadata_value(metadata, "source_revision_id")
        if len(source_revision_id) > 200 or not source_revision_id.isprintable():
            raise ValueError("invalid source revision metadata")
        input_key = _unique_metadata_value(metadata, "input_key")
        input_kind = _unique_metadata_value(metadata, "input_kind")
        input_sha256 = _unique_metadata_value(metadata, "input_sha256")
        document = ProviderDocument(
            store_id=store.id,
            provider="gemini",
            provider_document_id=provider_document_id,
            source_revision_id=source_revision_id,
            input_key=input_key,
            input_kind=input_kind,
            input_sha256=input_sha256,
            provider_file_name=_optional_text(_value(value, "file_name", "fileName", "file")),
            provider_document_name=_optional_text(
                _value(value, "display_name", "displayName", "document_name", "documentName")
            ),
            provider_operation_name=_optional_text(
                _value(value, "operation_name", "operationName", "operation")
            ),
            input_byte_count=_byte_count(value),
            metadata=metadata,
            state=IndexState.READY,
        )
    except (GeminiContractError, ValueError):
        return RemoteDocumentObservation(
            provider_document_id=provider_document_id,
            validation_error="invalid_metadata",
        )
    return RemoteDocumentObservation(
        provider_document_id=provider_document_id,
        source_revision_id=document.source_revision_id,
        input_key=document.input_key,
        input_kind=document.input_kind,
        input_sha256=document.input_sha256,
        input_byte_count=document.input_byte_count,
        provider_file_name=document.provider_file_name,
        provider_document_name=document.provider_document_name,
        provider_operation_name=document.provider_operation_name,
        metadata_json=document.metadata_json,
    )


def _byte_count(value: object) -> int | None:
    size_value = _value(value, "size_bytes", "sizeBytes", "input_byte_count", "bytes")
    return size_value if isinstance(size_value, int) and size_value >= 0 else None


def _require_source_revision_id(metadata: object) -> str:
    source_revision_id = _metadata_value(metadata, "source_revision_id")
    if (
        source_revision_id is None
        or not source_revision_id
        or len(source_revision_id) > 200
        or not source_revision_id.isprintable()
    ):
        raise GeminiContractError(
            "Gemini document metadata omitted a valid source revision identity."
        )
    return source_revision_id


def _json_metadata(value: object) -> object:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): _json_metadata(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_metadata(item) for item in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    key = _safe_attr(value, "key")
    if isinstance(key, str) and key.strip():
        for value_name in ("string_value", "stringValue", "numeric_value", "numericValue"):
            item = _safe_attr(value, value_name)
            if item is not None:
                return {key: _json_metadata(item)}
    return {}


def _metadata_value(metadata: object, name: str) -> str | None:
    if isinstance(metadata, Mapping):
        value = metadata.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        key = metadata.get("key")
        if key == name:
            for value_name in ("string_value", "stringValue", "numeric_value", "numericValue"):
                item = metadata.get(value_name)
                if isinstance(item, (str, int, float)):
                    return str(item)
        for nested in metadata.values():
            found = _metadata_value(nested, name)
            if found is not None:
                return found
    if isinstance(metadata, list):
        for item in metadata:
            found = _metadata_value(item, name)
            if found is not None:
                return found
    return None


def _unique_metadata_value(metadata: object, name: str) -> str:
    values = _metadata_values(metadata, name)
    if len(values) != 1:
        raise ValueError(f"Gemini metadata requires exactly one {name}")
    return values[0]


def _metadata_values(metadata: object, name: str) -> list[str]:
    values: list[str] = []
    if isinstance(metadata, Mapping):
        direct = metadata.get(name)
        if isinstance(direct, str) and direct.strip():
            values.append(direct.strip())
        if metadata.get("key") == name:
            for value_name in ("string_value", "stringValue", "numeric_value", "numericValue"):
                item = metadata.get(value_name)
                if isinstance(item, (str, int, float)) and str(item).strip():
                    values.append(str(item).strip())
        for nested in metadata.values():
            values.extend(_metadata_values(nested, name))
    elif isinstance(metadata, list):
        for item in metadata:
            values.extend(_metadata_values(item, name))
    return values


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        return None
    return value.strip()[:500]


def _value(value: object, *names: str) -> object | None:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return cast(object, value[name])
        return None
    for name in names:
        candidate = _safe_attr(value, name)
        if candidate is not None:
            return candidate
    return None


def _safe_attr(value: object, name: str) -> object | None:
    try:
        return getattr(value, name, None)
    except Exception:
        return None


__all__ = [
    "CompletedOperation",
    "GeminiFileSearchAdmin",
    "OperationRef",
    "RemoteDocumentObservation",
    "UploadedFileRef",
]
