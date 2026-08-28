#!/usr/bin/env python3
"""Bounded cleanup for Task 2.8 disposable Gemini resources only."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import re
from collections.abc import AsyncIterable, Callable
from typing import Any

from pydantic import SecretStr

from oms_hub.providers.gemini.client import GeminiClientFactory, translate_gemini_error
from oms_hub.providers.gemini.models import GeminiConfig
from oms_hub.security.secret_store import KeyringSecretStore, SecretStore

_SDK_VERSION = "2.14.0"
_MODEL_CONTRACT = (
    _SDK_VERSION,
    "gemini-3.7-flash",
    "models/gemini-embedding-2",
    "v1beta",
)
_STORE_NAME = re.compile(r"task-2-8-private-[0-9a-f]{32}").fullmatch
_FILE_NAME = re.compile(r"task-2-8-private-[0-9a-f]{32}-[0-9]{3}").fullmatch
_MAX_INSPECTED = 1_000
_MAX_STORES = 32
_MAX_FILES = 512
_MAX_DOCUMENTS = 1_000


class GoogleGenaiReconciliationSession:
    """Pinned-SDK operations needed by the bounded reconciliation."""

    model_contract = _MODEL_CONTRACT

    def __init__(self, api_key: str) -> None:
        config = GeminiConfig(api_key=SecretStr(api_key))
        self._clients = GeminiClientFactory(config)

    async def list_stores(self) -> tuple[object, ...]:
        async with self._clients.client() as client:
            listed = await client.file_search_stores.list(config={"page_size": 100})
            return await _collect(listed)

    async def list_files(self) -> tuple[object, ...]:
        async with self._clients.client() as client:
            listed = await client.files.list(config={"page_size": 100})
            return await _collect(listed)

    async def list_documents(self, store_name: str) -> tuple[object, ...]:
        async with self._clients.client() as client:
            listed = await client.file_search_stores.documents.list(parent=store_name)
            return await _collect(listed)

    async def delete_document(self, name: str) -> None:
        async with self._clients.client() as client:
            await client.file_search_stores.documents.delete(
                name=name, config={"force": True}
            )

    async def delete_file(self, name: str) -> None:
        async with self._clients.client() as client:
            await client.files.delete(name=name)

    async def delete_store(self, name: str) -> None:
        async with self._clients.client() as client:
            await client.file_search_stores.delete(name=name, config={"force": True})


async def _collect(items: object) -> tuple[object, ...]:
    if not isinstance(items, AsyncIterable):
        raise TypeError("Gemini list response was not async iterable")
    collected: list[object] = []
    async for item in items:
        collected.append(item)
        if len(collected) > _MAX_INSPECTED:
            raise OverflowError("Gemini reconciliation inventory exceeded its bound")
    return tuple(collected)


def _field(item: object, name: str) -> object:
    try:
        return getattr(item, name)
    except Exception as error:
        raise TypeError("Gemini reconciliation identity was unavailable") from error


def _owned(items: tuple[object, ...], matcher: Callable[[str], object]) -> list[object]:
    owned: list[object] = []
    for item in items:
        display_name = _field(item, "display_name")
        if display_name is None:
            continue
        if not isinstance(display_name, str):
            raise TypeError("Gemini reconciliation display name was invalid")
        if matcher(display_name) is not None:
            _identity(item)
            owned.append(item)
    return owned


def _identity(item: object) -> str:
    name = _field(item, "name")
    if not isinstance(name, str) or not name:
        raise TypeError("Gemini reconciliation resource identity was invalid")
    return name


def _full_result(
    *,
    status: str,
    states: list[str],
    inspected: dict[str, int],
    matched: dict[str, int],
    attempted: dict[str, int],
    remaining: dict[str, int],
    cleanup_complete: bool,
    warnings: list[str],
    inventory_failure_stage: str = "not_applicable",
    provider_error_category: str = "none",
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "status": status,
        "provider_operation_states": states,
        "inspected_counts": inspected,
        "matched_counts": matched,
        "delete_attempt_counts": attempted,
        "remaining_counts": remaining,
        "provider_cleanup_complete": cleanup_complete,
        "inventory_failure_stage": inventory_failure_stage,
        "provider_error_category": provider_error_category,
        "warnings": warnings,
    }


def _blocked_inventory(
    warning: str,
    *,
    inventory_failure_stage: str = "not_applicable",
    provider_error_category: str = "none",
) -> dict[str, object]:
    return _full_result(
        status="blocked",
        states=["inventory_failed"],
        inspected={"stores": 0, "files": 0, "documents": 0},
        matched={"stores": 0, "files": 0, "documents": 0},
        attempted={"stores": 0, "files": 0, "documents": 0},
        remaining={
            "stores": 0,
            "files": 0,
            "documents": 0,
            "stores_inspected": 0,
            "files_inspected": 0,
            "documents_inspected": 0,
        },
        cleanup_complete=False,
        warnings=[warning],
        inventory_failure_stage=inventory_failure_stage,
        provider_error_category=provider_error_category,
    )


def _blocked_provider_inventory(stage: str, error: Exception) -> dict[str, object]:
    return _blocked_inventory(
        "provider_reconciliation_incomplete",
        inventory_failure_stage=stage,
        provider_error_category=translate_gemini_error(error).category,
    )


async def reconcile_resources(session: Any) -> dict[str, object]:
    """Inventory first, then delete only exact Task 2.8 disposable resources."""

    try:
        stores = await session.list_stores()
    except OverflowError:
        return _blocked_inventory("provider_reconciliation_scope_exceeded")
    except Exception as error:
        return _blocked_provider_inventory("store_list", error)
    try:
        files = await session.list_files()
    except OverflowError:
        return _blocked_inventory("provider_reconciliation_scope_exceeded")
    except Exception as error:
        return _blocked_provider_inventory("file_list", error)
    try:
        owned_stores = _owned(stores, _STORE_NAME)
        owned_files = _owned(files, _FILE_NAME)
        if len(owned_stores) > _MAX_STORES or len(owned_files) > _MAX_FILES:
            return _blocked_inventory("provider_reconciliation_scope_exceeded")
        documents: dict[str, tuple[object, ...]] = {}
        for store in owned_stores:
            store_name = _identity(store)
            try:
                documents[store_name] = await session.list_documents(store_name)
            except OverflowError:
                return _blocked_inventory("provider_reconciliation_scope_exceeded")
            except Exception as error:
                return _blocked_provider_inventory("document_list", error)
        owned_documents = [item for values in documents.values() for item in values]
        if len(owned_documents) > _MAX_DOCUMENTS:
            return _blocked_inventory("provider_reconciliation_scope_exceeded")
        for document in owned_documents:
            _identity(document)
    except OverflowError:
        return _blocked_inventory("provider_reconciliation_scope_exceeded")
    except (AttributeError, TypeError, ValueError):
        return _blocked_inventory("provider_reconciliation_contract_invalid")
    except Exception:
        return _blocked_inventory("provider_reconciliation_contract_invalid")

    inspected = {
        "stores": len(stores),
        "files": len(files),
        "documents": len(owned_documents),
    }
    matched = {
        "stores": len(owned_stores),
        "files": len(owned_files),
        "documents": len(owned_documents),
    }
    attempted = {"stores": 0, "files": 0, "documents": 0}
    incomplete = False

    for document in owned_documents:
        attempted["documents"] += 1
        try:
            await session.delete_document(_identity(document))
        except Exception:
            incomplete = True

    remaining_documents = 0
    documents_inspected = 0
    for store in owned_stores:
        try:
            relisted = await session.list_documents(_identity(store))
            documents_inspected += len(relisted)
            remaining_documents += len(relisted)
        except Exception:
            incomplete = True
            remaining_documents += len(documents[_identity(store)])

    for file in owned_files:
        attempted["files"] += 1
        try:
            await session.delete_file(_identity(file))
        except Exception:
            incomplete = True
    for store in owned_stores:
        attempted["stores"] += 1
        try:
            await session.delete_store(_identity(store))
        except Exception:
            incomplete = True

    try:
        final_stores = await session.list_stores()
        final_files = await session.list_files()
        remaining_stores = len(_owned(final_stores, _STORE_NAME))
        remaining_files = len(_owned(final_files, _FILE_NAME))
    except Exception:
        incomplete = True
        final_stores = ()
        final_files = ()
        remaining_stores = len(owned_stores)
        remaining_files = len(owned_files)

    remaining = {
        "stores": remaining_stores,
        "files": remaining_files,
        "documents": remaining_documents,
        "stores_inspected": len(final_stores),
        "files_inspected": len(final_files),
        "documents_inspected": documents_inspected,
    }
    complete = not incomplete and not any(
        remaining[key] for key in ("stores", "files", "documents")
    )
    return _full_result(
        status="passed" if complete else "blocked",
        states=[
            "inventory_complete",
            "deletes_attempted",
            "reconciliation_empty" if complete else "reconciliation_incomplete",
        ],
        inspected=inspected,
        matched=matched,
        attempted=attempted,
        remaining=remaining,
        cleanup_complete=complete,
        warnings=[] if complete else ["provider_reconciliation_incomplete"],
    )


async def run_authorized_reconciliation(
    *,
    secret_store: SecretStore | None = None,
    session_factory: Callable[[str], Any] = GoogleGenaiReconciliationSession,
) -> dict[str, object]:
    """Cross the credential/provider boundary only after exact local opt-in."""

    if os.getenv("RUN_GEMINI_RECONCILIATION") != "1":
        return _full_result(
            status="blocked",
            states=["reconciliation_failed"],
            inspected={"stores": 0, "files": 0, "documents": 0},
            matched={"stores": 0, "files": 0, "documents": 0},
            attempted={"stores": 0, "files": 0, "documents": 0},
            remaining={
                "stores": 0,
                "files": 0,
                "documents": 0,
                "stores_inspected": 0,
                "files_inspected": 0,
                "documents_inspected": 0,
            },
            cleanup_complete=False,
            warnings=["provider_reconciliation_not_authorized"],
        )
    if importlib.metadata.version("google-genai") != _SDK_VERSION:
        return _blocked_inventory("provider_reconciliation_sdk_mismatch")
    secrets = secret_store if secret_store is not None else KeyringSecretStore()
    try:
        api_key = secrets.get("gemini-api-key")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError
        session = session_factory(api_key)
        if session.model_contract != _MODEL_CONTRACT:
            return _blocked_inventory("provider_reconciliation_model_mismatch")
        return await reconcile_resources(session)
    except Exception:
        return _blocked_inventory("provider_reconciliation_failed")


def main() -> int:
    result = asyncio.run(run_authorized_reconciliation())
    print(json.dumps(result, sort_keys=True, separators=(",", ":")), flush=True)
    return 0 if result.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
