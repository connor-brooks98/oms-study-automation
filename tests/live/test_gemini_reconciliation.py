from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run-gemini-reconciliation.py"


class _SdkError(RuntimeError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = {"x-request-id": "private-request-id"}


def _sdk_types() -> Any:
    from google.genai import types

    return types


def _load_operator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gemini_reconciliation", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeSession:
    model_contract = (
        "2.14.0",
        "gemini-3.7-flash",
        "models/gemini-embedding-2",
        "v1beta",
    )

    def __init__(
        self,
        *,
        stores: list[Any],
        files: list[Any],
        documents: dict[str, list[Any]],
        delete_document: bool = True,
        fail_document_relist: bool = False,
        fail_inventory_stage: str | None = None,
        inventory_error: Exception | None = None,
    ) -> None:
        self.stores = stores
        self.files = files
        self.documents = documents
        self.delete_document_enabled = delete_document
        self.fail_document_relist = fail_document_relist
        self.fail_inventory_stage = fail_inventory_stage
        self.inventory_error = inventory_error
        self.document_lists = 0
        self.calls: list[tuple[str, str]] = []

    async def list_stores(self) -> tuple[Any, ...]:
        if self.fail_inventory_stage == "store_untagged":
            assert self.inventory_error is not None
            raise self.inventory_error
        return tuple(self.stores)

    async def list_files(self) -> tuple[Any, ...]:
        if self.fail_inventory_stage == "file_list":
            assert self.inventory_error is not None
            raise self.inventory_error
        return tuple(self.files)

    async def list_documents(self, store_name: str) -> tuple[Any, ...]:
        self.document_lists += 1
        if self.fail_inventory_stage == "document_list" and self.document_lists == 1:
            assert self.inventory_error is not None
            raise self.inventory_error
        if self.fail_document_relist and self.document_lists == 2:
            raise RuntimeError("synthetic relist failure")
        return tuple(self.documents.get(store_name, ()))

    async def delete_document(self, name: str) -> None:
        self.calls.append(("delete_document", name))
        if self.delete_document_enabled:
            for items in self.documents.values():
                items[:] = [item for item in items if item.name != name]

    async def delete_file(self, name: str) -> None:
        self.calls.append(("delete_file", name))
        self.files[:] = [item for item in self.files if item.name != name]

    async def delete_store(self, name: str) -> None:
        self.calls.append(("delete_store", name))
        self.stores[:] = [item for item in self.stores if item.name != name]


def _owned_session(
    *,
    delete_document: bool = True,
    fail_document_relist: bool = False,
    fail_inventory_stage: str | None = None,
    inventory_error: Exception | None = None,
) -> _FakeSession:
    types = _sdk_types()
    token = "a" * 32
    store_name = "fileSearchStores/owned"
    return _FakeSession(
        stores=[
            types.FileSearchStore(
                name=store_name,
                display_name=f"task-2-8-private-{token}",
            ),
            types.FileSearchStore(
                name="fileSearchStores/foreign",
                display_name=f"task-2-8-private-{token}-extra",
            ),
        ],
        files=[
            types.File(
                name="files/owned",
                display_name=f"task-2-8-private-{token}-001",
            ),
            types.File(
                name="files/foreign",
                display_name=f"task-2-8-private-{token}-x01",
            ),
        ],
        documents={store_name: [types.Document(name=f"{store_name}/documents/owned")]},
        delete_document=delete_document,
        fail_document_relist=fail_document_relist,
        fail_inventory_stage=fail_inventory_stage,
        inventory_error=inventory_error,
    )


def test_reconciliation_deletes_only_exact_disposable_sdk_resources() -> None:
    operator = _load_operator()
    session = _owned_session()

    record = asyncio.run(operator.reconcile_resources(session))

    assert record == {
        "schema_version": 2,
        "status": "passed",
        "provider_operation_states": [
            "inventory_complete",
            "deletes_attempted",
            "reconciliation_empty",
        ],
        "inspected_counts": {"stores": 2, "files": 2, "documents": 1},
        "matched_counts": {"stores": 1, "files": 1, "documents": 1},
        "delete_attempt_counts": {"stores": 1, "files": 1, "documents": 1},
        "remaining_counts": {
            "stores": 0,
            "files": 0,
            "documents": 0,
            "stores_inspected": 1,
            "files_inspected": 1,
            "documents_inspected": 0,
        },
        "provider_cleanup_complete": True,
        "inventory_failure_stage": "not_applicable",
        "provider_error_category": "none",
        "warnings": [],
    }
    assert [item.name for item in session.stores] == ["fileSearchStores/foreign"]
    assert [item.name for item in session.files] == ["files/foreign"]
    assert "owned" not in json.dumps(record, sort_keys=True)


def test_document_noop_blocks_before_force_store_deletion_can_hide_it() -> None:
    operator = _load_operator()
    session = _owned_session(delete_document=False)

    record = asyncio.run(operator.reconcile_resources(session))

    assert record["status"] == "blocked"
    assert record["remaining_counts"]["documents"] == 1
    assert record["provider_cleanup_complete"] is False
    assert [name for action, name in session.calls if action == "delete_store"] == [
        "fileSearchStores/owned"
    ]


def test_document_relist_failure_still_attempts_known_file_and_store_cleanup() -> None:
    operator = _load_operator()
    session = _owned_session(fail_document_relist=True)

    record = asyncio.run(operator.reconcile_resources(session))

    assert record["status"] == "blocked"
    assert record["warnings"] == ["provider_reconciliation_incomplete"]
    assert ("delete_file", "files/owned") in session.calls
    assert ("delete_store", "fileSearchStores/owned") in session.calls


def test_scope_cap_fails_before_any_mutation() -> None:
    operator = _load_operator()
    types = _sdk_types()
    stores = [
        types.FileSearchStore(
            name=f"fileSearchStores/owned-{index}",
            display_name=f"task-2-8-private-{index:032x}",
        )
        for index in range(33)
    ]
    session = _FakeSession(stores=stores, files=[], documents={})

    record = asyncio.run(operator.reconcile_resources(session))

    assert record["status"] == "blocked"
    assert record["warnings"] == ["provider_reconciliation_scope_exceeded"]
    assert record["inventory_failure_stage"] == "not_applicable"
    assert record["provider_error_category"] == "none"
    assert session.calls == []


@pytest.mark.parametrize(
    ("stage", "category", "error"),
    (
        (
            "file_list",
            "quota",
            _SdkError("private quota payload", status_code=429),
        ),
        (
            "document_list",
            "transient",
            TimeoutError("private transient payload"),
        ),
        (
            "file_list",
            "provider",
            RuntimeError("private provider payload"),
        ),
    ),
)
def test_inventory_failure_reports_only_safe_stage_and_existing_error_category(
    stage: str,
    category: str,
    error: Exception,
) -> None:
    operator = _load_operator()
    session = _owned_session(fail_inventory_stage=stage, inventory_error=error)

    record = asyncio.run(operator.reconcile_resources(session))

    assert record["schema_version"] == 2
    assert record["status"] == "blocked"
    assert record["provider_operation_states"] == ["inventory_failed"]
    assert record["warnings"] == ["provider_reconciliation_incomplete"]
    assert record["inventory_failure_stage"] == stage
    assert record["provider_error_category"] == category
    assert session.calls == []
    serialized = json.dumps(record, sort_keys=True)
    for forbidden in ("private", "request-id", "401", "429", "503"):
        assert forbidden not in serialized


def _real_factory_session(
    operator: ModuleType,
    *,
    list_stores: Callable[..., Any],
    close: Callable[[], Any],
) -> Any:
    aio = SimpleNamespace(
        file_search_stores=SimpleNamespace(list=list_stores),
        aclose=close,
    )
    clients = operator.GeminiClientFactory(
        operator.GeminiConfig(api_key=operator.SecretStr("synthetic-key")),
        sdk_factory=lambda **kwargs: SimpleNamespace(aio=aio),
    )
    session = object.__new__(operator.GoogleGenaiReconciliationSession)
    session._clients = clients
    return session


def _real_sdk_transport_session(operator: ModuleType, *, status_code: int) -> Any:
    from google import genai
    from google.genai import types

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={
                "error": {
                    "code": status_code,
                    "message": "private-body-marker",
                    "status": "PRIVATE_STATUS",
                }
            },
            headers={"x-request-id": "private-request-id-marker"},
        )

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    sdk_client = genai.Client(
        api_key="synthetic-key",
        http_options=types.HttpOptions(
            api_version="v1beta",
            base_url="https://unit.invalid",
            httpx_async_client=async_client,
        ),
    )
    clients = operator.GeminiClientFactory(
        operator.GeminiConfig(api_key=operator.SecretStr("synthetic-key")),
        sdk_factory=lambda **kwargs: sdk_client,
    )
    session = object.__new__(operator.GoogleGenaiReconciliationSession)
    session._clients = clients
    return session


def _assert_safe_store_failure(record: dict[str, object], stage: str, category: str) -> None:
    assert record["schema_version"] == 2
    assert record["status"] == "blocked"
    assert record["provider_operation_states"] == ["inventory_failed"]
    assert record["inventory_failure_stage"] == stage
    assert record["provider_error_category"] == category
    assert record["delete_attempt_counts"] == {"stores": 0, "files": 0, "documents": 0}
    serialized = json.dumps(record, sort_keys=True)
    for forbidden in ("private", "request-id", "401", "429", "503"):
        assert forbidden not in serialized


def test_store_client_construction_failure_is_categorized_without_payload() -> None:
    operator = _load_operator()

    def fail_construction(**kwargs: object) -> object:
        raise _SdkError("private construction payload", status_code=401)

    clients = operator.GeminiClientFactory(
        operator.GeminiConfig(api_key=operator.SecretStr("synthetic-key")),
        sdk_factory=fail_construction,
    )
    session = object.__new__(operator.GoogleGenaiReconciliationSession)
    session._clients = clients

    record = asyncio.run(operator.reconcile_resources(session))

    _assert_safe_store_failure(record, "store_client", "authentication")


@pytest.mark.parametrize("failure_site", ["request", "pager"])
def test_store_request_and_pager_failures_share_request_stage(failure_site: str) -> None:
    operator = _load_operator()
    calls: list[str] = []

    async def failing_pager() -> AsyncIterator[object]:
        calls.append("pager")
        raise TimeoutError("private pager payload")
        yield object()

    async def list_stores(**kwargs: object) -> object:
        calls.append("request")
        if failure_site == "request":
            raise TimeoutError("private request payload")
        return failing_pager()

    async def close() -> None:
        calls.append("close")

    session = _real_factory_session(operator, list_stores=list_stores, close=close)

    record = asyncio.run(operator.reconcile_resources(session))

    _assert_safe_store_failure(record, "store_request", "transient")
    assert calls[-1] == "close"


@pytest.mark.parametrize(
    ("status_code", "category"),
    ((400, "provider_bad_request"), (404, "provider_not_found")),
)
def test_real_sdk_store_request_projects_only_safe_provider_status(
    status_code: int,
    category: str,
) -> None:
    operator = _load_operator()
    session = _real_sdk_transport_session(operator, status_code=status_code)

    record = asyncio.run(operator.reconcile_resources(session))

    _assert_safe_store_failure(record, "store_request", category)
    serialized = json.dumps(record, sort_keys=True)
    for forbidden in (
        "private-body-marker",
        "PRIVATE_STATUS",
        "private-request-id-marker",
        str(status_code),
    ):
        assert forbidden not in serialized


def test_store_close_failure_is_categorized_after_successful_collection() -> None:
    operator = _load_operator()

    async def empty_pager() -> AsyncIterator[object]:
        if False:
            yield object()

    async def list_stores(**kwargs: object) -> object:
        return empty_pager()

    async def close() -> None:
        raise RuntimeError("private close payload")

    session = _real_factory_session(operator, list_stores=list_stores, close=close)

    record = asyncio.run(operator.reconcile_resources(session))

    _assert_safe_store_failure(record, "store_close", "provider")


def test_store_close_failure_does_not_mask_primary_request_failure() -> None:
    operator = _load_operator()

    async def list_stores(**kwargs: object) -> object:
        raise TimeoutError("private primary payload")

    async def close() -> None:
        raise RuntimeError("private close payload")

    session = _real_factory_session(operator, list_stores=list_stores, close=close)

    record = asyncio.run(operator.reconcile_resources(session))

    _assert_safe_store_failure(record, "store_request", "transient")


def test_store_collection_overflow_preserves_scope_failure() -> None:
    operator = _load_operator()

    async def oversized_pager() -> AsyncIterator[object]:
        for _ in range(1_001):
            yield object()

    async def list_stores(**kwargs: object) -> object:
        return oversized_pager()

    async def close() -> None:
        return None

    session = _real_factory_session(operator, list_stores=list_stores, close=close)

    record = asyncio.run(operator.reconcile_resources(session))

    assert record["warnings"] == ["provider_reconciliation_scope_exceeded"]
    assert record["inventory_failure_stage"] == "not_applicable"
    assert record["provider_error_category"] == "none"


def test_untagged_store_failure_fails_closed_without_invented_stage() -> None:
    operator = _load_operator()
    session = _owned_session(
        fail_inventory_stage="store_untagged",
        inventory_error=RuntimeError("private untagged payload"),
    )

    record = asyncio.run(operator.reconcile_resources(session))

    assert record["warnings"] == ["provider_reconciliation_contract_invalid"]
    assert record["inventory_failure_stage"] == "not_applicable"
    assert record["provider_error_category"] == "none"


@pytest.mark.parametrize("kind", ["store", "file", "document"])
def test_optional_sdk_identity_none_blocks_before_mutation(kind: str) -> None:
    operator = _load_operator()
    session = _owned_session()
    if kind == "store":
        session.stores[0].name = None
    elif kind == "file":
        session.files[0].name = None
    else:
        session.documents["fileSearchStores/owned"][0].name = None

    record = asyncio.run(operator.reconcile_resources(session))

    assert record["status"] == "blocked"
    assert record["warnings"] == ["provider_reconciliation_contract_invalid"]
    assert session.calls == []


def test_optional_display_name_none_is_ignored_as_foreign() -> None:
    operator = _load_operator()
    types = _sdk_types()
    session = _FakeSession(
        stores=[types.FileSearchStore(name="fileSearchStores/foreign", display_name=None)],
        files=[types.File(name="files/foreign", display_name=None)],
        documents={},
    )

    record = asyncio.run(operator.reconcile_resources(session))

    assert record["status"] == "passed"
    assert record["matched_counts"] == {"stores": 0, "files": 0, "documents": 0}
    assert session.calls == []


def test_live_session_awaits_pinned_sdk_lists_and_uses_exact_delete_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = _load_operator()
    types = _sdk_types()
    calls: list[tuple[str, object]] = []

    async def pager(item: object) -> AsyncIterator[object]:
        yield item

    async def list_stores(*, config: object) -> object:
        calls.append(("list_stores", config))
        return pager(types.FileSearchStore(name="fileSearchStores/one"))

    async def list_files(*, config: object) -> object:
        calls.append(("list_files", config))
        return pager(types.File(name="files/one"))

    async def list_documents(*, parent: str) -> object:
        calls.append(("list_documents", parent))
        return pager(types.Document(name=f"{parent}/documents/one"))

    async def delete_document(**kwargs: object) -> None:
        calls.append(("delete_document", kwargs))

    async def delete_file(**kwargs: object) -> None:
        calls.append(("delete_file", kwargs))

    async def delete_store(**kwargs: object) -> None:
        calls.append(("delete_store", kwargs))

    client = SimpleNamespace(
        file_search_stores=SimpleNamespace(
            list=list_stores,
            delete=delete_store,
            documents=SimpleNamespace(list=list_documents, delete=delete_document),
        ),
        files=SimpleNamespace(list=list_files, delete=delete_file),
    )

    class Factory:
        @asynccontextmanager
        async def client(self) -> AsyncIterator[SimpleNamespace]:
            yield client

    monkeypatch.setattr(operator, "GeminiClientFactory", lambda config: Factory())
    session = operator.GoogleGenaiReconciliationSession("synthetic-key")

    assert asyncio.run(session.list_stores())[0].name == "fileSearchStores/one"
    assert asyncio.run(session.list_files())[0].name == "files/one"
    assert (
        asyncio.run(session.list_documents("fileSearchStores/one"))[0].name
        == "fileSearchStores/one/documents/one"
    )
    asyncio.run(session.delete_document("document"))
    asyncio.run(session.delete_file("file"))
    asyncio.run(session.delete_store("store"))

    assert calls == [
        ("list_stores", {"page_size": 100}),
        ("list_files", {"page_size": 100}),
        ("list_documents", "fileSearchStores/one"),
        ("delete_document", {"name": "document", "config": {"force": True}}),
        ("delete_file", {"name": "file"}),
        ("delete_store", {"name": "store", "config": {"force": True}}),
    ]


class _FakeSecrets:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, key: str) -> str:
        self.calls.append(key)
        return "synthetic-reconciliation-key"


def test_authorized_boundary_reads_the_stored_key_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = _load_operator()
    secrets = _FakeSecrets()
    session = _FakeSession(stores=[], files=[], documents={})
    received: list[str] = []
    monkeypatch.setenv("RUN_GEMINI_RECONCILIATION", "1")
    monkeypatch.setattr(operator.importlib.metadata, "version", lambda name: "2.14.0")

    def session_factory(key: str) -> _FakeSession:
        received.append(key)
        return session

    record = asyncio.run(
        operator.run_authorized_reconciliation(
            secret_store=secrets,
            session_factory=session_factory,
        )
    )

    assert record["status"] == "passed"
    assert secrets.calls == ["gemini-api-key"]
    assert received == ["synthetic-reconciliation-key"]


def test_missing_opt_in_fails_before_key_or_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = _load_operator()
    secrets = _FakeSecrets()
    monkeypatch.delenv("RUN_GEMINI_RECONCILIATION", raising=False)

    record = asyncio.run(
        operator.run_authorized_reconciliation(
            secret_store=secrets,
            session_factory=lambda key: pytest.fail("provider boundary crossed"),
        )
    )

    assert record == {
        "schema_version": 2,
        "status": "blocked",
        "provider_operation_states": ["reconciliation_failed"],
        "inspected_counts": {"stores": 0, "files": 0, "documents": 0},
        "matched_counts": {"stores": 0, "files": 0, "documents": 0},
        "delete_attempt_counts": {"stores": 0, "files": 0, "documents": 0},
        "remaining_counts": {
            "stores": 0,
            "files": 0,
            "documents": 0,
            "stores_inspected": 0,
            "files_inspected": 0,
            "documents_inspected": 0,
        },
        "provider_cleanup_complete": False,
        "inventory_failure_stage": "not_applicable",
        "provider_error_category": "none",
        "warnings": ["provider_reconciliation_not_authorized"],
    }
    assert secrets.calls == []


def test_committed_operator_has_no_private_source_or_creation_path() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    for forbidden in (
        "source_trust_schema29",
        "run_authorized_private_shadow",
        "upload_input",
        "import_input",
        "query_private",
        "create_store",
        "Lecture 13",
    ):
        assert forbidden not in source
