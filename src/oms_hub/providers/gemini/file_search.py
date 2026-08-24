"""Local-only Gemini File Search store and document administration."""

from __future__ import annotations

from collections.abc import AsyncIterable, Iterable, Mapping
from typing import Any, cast

from oms_hub.db import Database
from oms_hub.indexing.models import (
    IndexState,
    ProviderDocument,
    ProviderStore,
    StoreKey,
)
from oms_hub.indexing.repository import IndexRepository
from oms_hub.providers.gemini.client import GeminiClientFactory, translate_gemini_error
from oms_hub.providers.gemini.errors import GeminiContractError, GeminiProviderError


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

    async def ensure_store(self, key: StoreKey) -> ProviderStore:
        key = _require_store_key(key)
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
            documents_api = _documents_api(client)
            raw_documents = await _call_provider(
                documents_api.list,
                parent=store.provider_store_name,
            )
            entries = await _collect_documents(raw_documents)

        mapped: list[ProviderDocument] = []
        for entry in entries:
            mapped.append(_document_from_provider(entry, store))
        mapped.sort(key=lambda item: (item.provider_document_id, item.id))
        return tuple(self.repository.upsert_document(item) for item in mapped)

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
            await _call_provider(documents_api.delete, name=document.provider_document_id)
        self.repository.mark_document_deleted(provider_document_id)


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


async def _call_provider(method: Any, **kwargs: object) -> Any:
    try:
        result = method(**kwargs)
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
    source_revision_id = _metadata_value(metadata, "source_revision_id")
    if not source_revision_id:
        source_revision_id = f"provider:{provider_document_id}"
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


__all__ = ["GeminiFileSearchAdmin"]
