from __future__ import annotations

import asyncio
import json
from importlib import import_module
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from oms_hub.db import Database
from oms_hub.providers.gemini.client import GeminiClientFactory
from oms_hub.providers.gemini.errors import (
    GeminiContractError,
    GeminiProviderError,
    GeminiTransientError,
)
from oms_hub.providers.gemini.file_search import (
    CompletedOperation,
    GeminiFileSearchAdmin,
    OperationRef,
    UploadedFileRef,
    build_import_file_config,
)
from oms_hub.providers.gemini.models import GeminiConfig


class FakeFiles:
    def __init__(self) -> None:
        self.upload_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []
        self.delete_error: BaseException | None = None

    async def upload(self, **kwargs: object) -> object:
        self.upload_calls.append(kwargs)
        return SimpleNamespace(name="files/provider-1")

    async def delete(self, **kwargs: object) -> None:
        self.delete_calls.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error


class FakeStores:
    def __init__(self) -> None:
        self.import_calls: list[dict[str, object]] = []

    async def import_file(self, **kwargs: object) -> object:
        self.import_calls.append(kwargs)
        return SimpleNamespace(name="operations/import-1")


class FakeOperations:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.results: list[object] = []
        self.delay_seconds = 0.0

    async def get(self, operation: object) -> object:
        self.calls.append(str(getattr(operation, "name", None)))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.results.pop(0)


class FakeAioClient:
    def __init__(self) -> None:
        self.files = FakeFiles()
        self.file_search_stores = FakeStores()
        self.operations = FakeOperations()
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class FakeSdkClient:
    def __init__(self, aio: FakeAioClient) -> None:
        self.aio = aio


def run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def admin_bundle(
    *,
    operation_poll_seconds: int = 2,
    operation_timeout_seconds: int = 900,
) -> tuple[GeminiFileSearchAdmin, FakeAioClient]:
    client = FakeAioClient()
    factory = GeminiClientFactory(
        GeminiConfig(
            api_key=SecretStr("provider-secret"),
            operation_poll_seconds=operation_poll_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
        ),
        sdk_factory=lambda **_: FakeSdkClient(client),
    )
    return GeminiFileSearchAdmin(Database("sqlite://"), factory), client


def test_upload_import_and_cleanup_use_exact_async_sdk_contract(tmp_path: Path) -> None:
    admin, client = admin_bundle()
    source = tmp_path / "lecture.pptx"
    source.write_bytes(b"canonical bytes")
    metadata = [
        {"key": "authority_class", "string_value": "course_material"},
        {"key": "source_revision_id", "string_value": "sr_1"},
    ]
    chunking = {
        "white_space_config": {
            "max_tokens_per_chunk": 700,
            "max_overlap_tokens": 100,
        }
    }

    uploaded = run(admin.upload_file(source, "lecture.pptx"))
    operation = run(
        admin.import_file(
            "fileSearchStores/course-1",
            uploaded.name,
            metadata,
            chunking,
        )
    )
    run(admin.delete_file(uploaded.name))

    assert uploaded == UploadedFileRef(name="files/provider-1", size_bytes=15)
    assert operation == OperationRef(name="operations/import-1")
    assert client.files.upload_calls == [
        {"file": source, "config": {"display_name": "lecture.pptx"}}
    ]
    import_call = client.file_search_stores.import_calls
    assert len(import_call) == 1
    assert import_call[0]["file_search_store_name"] == "fileSearchStores/course-1"
    assert import_call[0]["file_name"] == "files/provider-1"
    assert import_call[0]["config"].http_options.extra_body == {
        "customMetadata": [
            {"key": "authority_class", "stringValue": "course_material"},
            {"key": "source_revision_id", "stringValue": "sr_1"},
        ],
        "chunkingConfig": {
            "whiteSpaceConfig": {
                "maxTokensPerChunk": 700,
                "maxOverlapTokens": 100,
            }
        },
    }
    assert client.files.delete_calls == [{"name": "files/provider-1"}]
    assert client.close_calls == 3


@pytest.mark.parametrize(
    ("file_name", "metadata", "chunking", "expected"),
    (
        (
            "files/lecture-pdf",
            [{"key": "input_key", "string_value": "lecture_pdf"}],
            None,
            {
                "fileName": "files/lecture-pdf",
                "customMetadata": [
                    {"key": "input_key", "stringValue": "lecture_pdf"}
                ],
            },
        ),
        (
            "files/normalized-markdown",
            [{"key": "input_key", "string_value": "normalized_markdown"}],
            {
                "white_space_config": {
                    "max_tokens_per_chunk": 700,
                    "max_overlap_tokens": 100,
                }
            },
            {
                "fileName": "files/normalized-markdown",
                "customMetadata": [
                    {"key": "input_key", "stringValue": "normalized_markdown"}
                ],
                "chunkingConfig": {
                    "whiteSpaceConfig": {
                        "maxTokensPerChunk": 700,
                        "maxOverlapTokens": 100,
                    }
                },
            },
        ),
    ),
)
def test_real_sdk_import_preserves_public_config_wire_body(
    file_name: str,
    metadata: list[dict[str, str]],
    chunking: dict[str, object] | None,
    expected: dict[str, object],
) -> None:
    sdk = import_module("google.genai")
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"name": "operations/import-1", "done": False},
            request=request,
        )

    def sdk_factory(**kwargs: object) -> object:
        assert kwargs["api_key"] == "provider-secret"
        return sdk.Client(
            api_key="provider-secret",
            http_options={
                "api_version": "v1beta",
                "base_url": "https://unit.invalid",
                "httpx_async_client": httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ),
            },
        )

    factory = GeminiClientFactory(
        GeminiConfig(api_key=SecretStr("provider-secret")),
        sdk_factory=sdk_factory,
    )
    admin = GeminiFileSearchAdmin(Database("sqlite://"), factory)

    operation = run(
        admin.import_file(
            "fileSearchStores/course-1",
            file_name,
            metadata,
            chunking,
        )
    )

    assert operation == OperationRef(name="operations/import-1")
    assert captured == [expected]


@pytest.mark.parametrize(
    ("file_name", "metadata", "chunking", "expected"),
    (
        (
            "files/lecture-pdf",
            [{"key": "input_key", "string_value": "lecture_pdf"}],
            None,
            {
                "fileName": "files/lecture-pdf",
                "customMetadata": [
                    {"key": "input_key", "stringValue": "lecture_pdf"}
                ],
            },
        ),
        (
            "files/normalized-markdown",
            [{"key": "input_key", "string_value": "normalized_markdown"}],
            {
                "white_space_config": {
                    "max_tokens_per_chunk": 700,
                    "max_overlap_tokens": 100,
                }
            },
            {
                "fileName": "files/normalized-markdown",
                "customMetadata": [
                    {"key": "input_key", "stringValue": "normalized_markdown"}
                ],
                "chunkingConfig": {
                    "whiteSpaceConfig": {
                        "maxTokensPerChunk": 700,
                        "maxOverlapTokens": 100,
                    }
                },
            },
        ),
    ),
)
def test_typed_public_sdk_import_preserves_provider_wire_names(
    file_name: str,
    metadata: list[dict[str, str]],
    chunking: dict[str, object] | None,
    expected: dict[str, object],
) -> None:
    sdk = import_module("google.genai")
    captured: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"name": "operations/import-1", "done": False},
            request=request,
        )

    async def import_file() -> object:
        client = sdk.Client(
            api_key="provider-secret",
            http_options={
                "api_version": "v1beta",
                "base_url": "https://unit.invalid",
                "httpx_async_client": httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ),
            },
        )
        try:
            return await client.aio.file_search_stores.import_file(
                file_search_store_name="fileSearchStores/course-1",
                file_name=file_name,
                config=build_import_file_config(metadata, chunking),
            )
        finally:
            await client.aio.aclose()

    operation = run(import_file())

    assert operation.name == "operations/import-1"
    assert captured == [expected]


@pytest.mark.parametrize(
    ("message", "expected_reason"),
    (
        ("Unsupported MIME type: text/markdown", "unsupported_mime_type"),
        ("private provider argument detail", "invalid_argument"),
    ),
)
def test_real_sdk_import_retains_only_fixed_invalid_argument_diagnostic(
    message: str,
    expected_reason: str,
) -> None:
    sdk = import_module("google.genai")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": message,
                    "status": "INVALID_ARGUMENT",
                }
            },
            request=request,
        )

    def sdk_factory(**kwargs: object) -> object:
        assert kwargs["api_key"] == "provider-secret"
        return sdk.Client(
            api_key="provider-secret",
            http_options={
                "api_version": "v1beta",
                "base_url": "https://unit.invalid",
                "httpx_async_client": httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)
                ),
            },
        )

    admin = GeminiFileSearchAdmin(
        Database("sqlite://"),
        GeminiClientFactory(
            GeminiConfig(api_key=SecretStr("provider-secret")),
            sdk_factory=sdk_factory,
        ),
    )

    with pytest.raises(GeminiProviderError) as raised:
        run(
            admin.import_file(
                "fileSearchStores/course-1",
                "files/normalized-markdown",
                [{"key": "input_key", "string_value": "normalized_markdown"}],
                {
                    "white_space_config": {
                        "max_tokens_per_chunk": 700,
                        "max_overlap_tokens": 100,
                    }
                },
            )
        )

    assert raised.value.provider_status_code == 400
    assert raised.value.diagnostic_code == expected_reason
    assert message not in str(raised.value)


@pytest.mark.parametrize(
    "store_name",
    (
        "fileSearchStores/course-1/foreign",
        "fileSearchStores/course-1?alt=foreign",
        "fileSearchStores/course-1#import",
        "fileSearchStores/course-1:delete",
    ),
)
def test_import_rejects_noncanonical_store_before_transport(store_name: str) -> None:
    admin, client = admin_bundle()

    with pytest.raises(GeminiContractError, match="store name is invalid"):
        run(
            admin.import_file(
                store_name,
                "files/normalized-markdown",
                [{"key": "input_key", "string_value": "normalized_markdown"}],
                None,
            )
        )

    assert client.file_search_stores.import_calls == []


def test_wait_polls_persisted_name_with_bounded_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, client = admin_bundle(operation_poll_seconds=10, operation_timeout_seconds=40)
    client.operations.results = [
        SimpleNamespace(name="operations/import-1", done=False),
        SimpleNamespace(name="operations/import-1", done=False),
        SimpleNamespace(
            name="operations/import-1",
            done=True,
            response=SimpleNamespace(
                document_name="fileSearchStores/course-1/documents/document-1"
            ),
        ),
    ]
    elapsed = [0.0]
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        elapsed[0] += delay

    monkeypatch.setattr("oms_hub.providers.gemini.file_search.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "oms_hub.providers.gemini.file_search.monotonic", lambda: elapsed[0]
    )

    completed = run(admin.wait_for_operation("operations/import-1"))

    assert completed == CompletedOperation(
        name="operations/import-1",
        document_name="fileSearchStores/course-1/documents/document-1",
    )
    assert client.operations.calls == ["operations/import-1"] * 3
    assert delays == [10, 15]
    assert client.close_calls == 1


def test_wait_timeout_and_operation_failure_are_safe_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, client = admin_bundle(operation_poll_seconds=2, operation_timeout_seconds=3)
    client.operations.results = [SimpleNamespace(name="operations/import-1", done=False)] * 3
    elapsed = [0.0]

    async def fake_sleep(delay: float) -> None:
        elapsed[0] += delay

    monkeypatch.setattr("oms_hub.providers.gemini.file_search.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "oms_hub.providers.gemini.file_search.monotonic", lambda: elapsed[0]
    )

    with pytest.raises(GeminiTransientError, match="timed out"):
        run(admin.wait_for_operation("operations/import-1"))

    assert client.operations.calls
    assert elapsed[0] == 3

    failed_admin, failed_client = admin_bundle()
    failed_client.operations.results = [
        SimpleNamespace(
            name="operations/import-2",
            done=True,
            error={"code": 503, "message": "private provider payload"},
        )
    ]
    with pytest.raises(GeminiTransientError) as raised:
        run(failed_admin.wait_for_operation("operations/import-2"))
    assert "private provider payload" not in str(raised.value)


def test_wait_bounds_an_inflight_poll_by_the_operation_deadline() -> None:
    admin, client = admin_bundle(operation_timeout_seconds=1)
    client.operations.delay_seconds = 2
    client.operations.results = [SimpleNamespace(name="operations/import-1", done=False)]

    started = monotonic()
    with pytest.raises(GeminiTransientError, match="timed out"):
        run(admin.wait_for_operation("operations/import-1"))

    assert monotonic() - started < 1.5
