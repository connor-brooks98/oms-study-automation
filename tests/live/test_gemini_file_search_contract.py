from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run-gemini-contract-smoke.py"
GATE_RECORD = (
    ROOT / "artifacts" / "acceptance" / "grounded-learning" / "gate-2b-gemini-indexing.json"
)
LIVE_ENABLED = os.getenv("RUN_LIVE_GEMINI_TESTS") == "1"


def _load_smoke() -> ModuleType:
    assert SCRIPT.is_file(), "Task 2.8 smoke scaffold is missing"
    spec = importlib.util.spec_from_file_location("task_2_8_gemini_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeSession:
    def __init__(self, smoke: ModuleType, *, fail_import: bool = False) -> None:
        self.smoke = smoke
        self.fail_import = fail_import
        self.calls: list[tuple[str, object]] = []
        self.store_name = "fileSearchStores/raw-store-identity"
        self.file_name = "files/raw-file-identity"
        self.operation_name = "operations/raw-operation-identity"
        self.document_name = "fileSearchStores/raw-store-identity/documents/raw-document"

    async def create_store(self, display_name: str, embedding_model: str) -> str:
        self.calls.append(("create_store", (display_name, embedding_model)))
        return self.store_name

    async def upload_pdf(self, display_name: str, content: bytes) -> str:
        assert content.startswith(b"%PDF-")
        self.calls.append(("upload_pdf", (display_name, len(content))))
        return self.file_name

    async def import_file(
        self,
        store_name: str,
        file_name: str,
        metadata: tuple[tuple[str, str], ...],
    ) -> str:
        self.calls.append(("import_file", (store_name, file_name, metadata)))
        if self.fail_import:
            raise self.smoke.SmokeTemporaryFailure("synthetic temporary failure")
        return self.operation_name

    async def wait_for_import(self, operation_name: str) -> str:
        self.calls.append(("wait_for_import", operation_name))
        return self.document_name

    async def query(
        self,
        store_name: str,
        prompt: str,
        scope: object,
        *,
        response_schema: type[object],
        omit_thinking: bool,
    ) -> object:
        del prompt, response_schema
        self.calls.append(("query", (store_name, scope, omit_thinking)))
        if scope.lecture_id == self.smoke.SYNTHETIC_LECTURE_ID:  # type: ignore[attr-defined]
            return self.smoke.SmokeQueryResult(
                answer={"answer": self.smoke.SYNTHETIC_FACT, "supported": True},
                citations=(
                    self.smoke.SmokeCitation(
                        document_name=self.document_name,
                        page_number=1,
                        excerpt=self.smoke.SYNTHETIC_FACT,
                    ),
                ),
                input_tokens=11,
                output_tokens=7,
            )
        return self.smoke.SmokeQueryResult(
            answer={"answer": "", "supported": False},
            citations=(),
        )

    async def list_documents(self, store_name: str) -> tuple[str, ...]:
        self.calls.append(("list_documents", store_name))
        return (self.document_name,)

    async def delete_document(self, document_name: str) -> None:
        self.calls.append(("delete_document", document_name))

    async def delete_file(self, file_name: str) -> None:
        self.calls.append(("delete_file", file_name))

    async def delete_store(self, store_name: str) -> None:
        self.calls.append(("delete_store", store_name))


class _SdkDocuments:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def list(self, *, parent: str) -> tuple[object, ...]:
        self.calls.append(("list", parent))
        return (SimpleNamespace(name="fileSearchStores/sdk-store/documents/sdk-document"),)

    async def delete(self, *, name: str, config: object) -> None:
        self.calls.append(("delete", (name, config)))


class _SdkStores:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.documents = _SdkDocuments()

    async def create(self, *, config: object) -> object:
        self.calls.append(("create", config))
        return SimpleNamespace(name="fileSearchStores/sdk-store")

    async def import_file(
        self,
        *,
        file_search_store_name: str,
        file_name: str,
        config: object,
    ) -> object:
        self.calls.append(("import_file", (file_search_store_name, file_name, config)))
        return SimpleNamespace(name="operations/sdk-operation")

    async def delete(self, *, name: str, config: object) -> None:
        self.calls.append(("delete", (name, config)))


class _SdkFiles:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def upload(self, *, file: object, config: object) -> object:
        content = file.read()
        self.calls.append(("upload", (content, config)))
        return SimpleNamespace(name="files/sdk-file")

    async def delete(self, *, name: str) -> None:
        self.calls.append(("delete", name))


class _SdkOperations:
    def __init__(self, *, error_status: int | None = None, delay: float = 0.0) -> None:
        self.calls: list[object] = []
        self.error_status = error_status
        self.delay = delay

    async def get(self, operation: object) -> object:
        self.calls.append(operation)
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(
            name="operations/sdk-operation",
            done=True,
            error={"code": self.error_status} if self.error_status is not None else None,
            response=SimpleNamespace(
                document_name="fileSearchStores/sdk-store/documents/sdk-document"
            ),
        )


class _SdkModels:
    def __init__(self, smoke: ModuleType) -> None:
        self.smoke = smoke
        self.calls: list[tuple[str, object, object]] = []

    async def generate_content(self, *, model: str, contents: object, config: object) -> object:
        self.calls.append((model, contents, config))
        file_search = config["tools"][0]["file_search"]
        if self.smoke.WRONG_LECTURE_ID in file_search["metadata_filter"]:
            chunks: list[object] = []
            parsed = self.smoke.SmokeAnswer(answer="", supported=False)
        else:
            metadata = [
                SimpleNamespace(key=key, string_value=value)
                for key, value in (
                    ("course_id", self.smoke.SYNTHETIC_COURSE_ID),
                    ("exam_id", self.smoke.SYNTHETIC_EXAM_ID),
                    ("lecture_id", self.smoke.SYNTHETIC_LECTURE_ID),
                    ("source_revision_id", self.smoke.SYNTHETIC_REVISION_ID),
                    ("authority_class", "course_material"),
                    ("input_key", "pdf"),
                    ("input_kind", "pdf"),
                    (
                        "input_sha256",
                        hashlib.sha256(self.smoke.synthetic_pdf_bytes()).hexdigest(),
                    ),
                )
            ]
            chunks = [
                SimpleNamespace(
                    retrieved_context=SimpleNamespace(
                        text=self.smoke.SYNTHETIC_FACT,
                        custom_metadata=metadata,
                        file_search_store="fileSearchStores/sdk-store",
                        page_number=1,
                    )
                )
            ]
            parsed = self.smoke.SmokeAnswer(answer=self.smoke.SYNTHETIC_FACT, supported=True)
        return SimpleNamespace(
            parsed=parsed,
            candidates=[
                SimpleNamespace(
                    grounding_metadata=SimpleNamespace(grounding_chunks=chunks)
                )
            ],
            usage_metadata=SimpleNamespace(prompt_token_count=13, candidates_token_count=8),
        )


class _SdkAio:
    def __init__(
        self,
        smoke: ModuleType,
        *,
        operation_error_status: int | None = None,
        operation_delay: float = 0.0,
    ) -> None:
        self.file_search_stores = _SdkStores()
        self.files = _SdkFiles()
        self.operations = _SdkOperations(
            error_status=operation_error_status,
            delay=operation_delay,
        )
        self.models = _SdkModels(smoke)
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


class _SdkClient:
    def __init__(
        self,
        smoke: ModuleType,
        *,
        operation_error_status: int | None = None,
        operation_delay: float = 0.0,
    ) -> None:
        self.aio = _SdkAio(
            smoke,
            operation_error_status=operation_error_status,
            operation_delay=operation_delay,
        )


class _FakeSecrets:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.calls: list[str] = []

    def get(self, key: str) -> str | None:
        self.calls.append(key)
        return self.value


def test_google_genai_2_14_session_maps_exact_sdk_contract() -> None:
    smoke = _load_smoke()
    clients: list[_SdkClient] = []

    def sdk_factory(**kwargs: object) -> _SdkClient:
        assert kwargs == {
            "api_key": "synthetic-sdk-key",
            "http_options": {"api_version": "v1beta"},
        }
        client = _SdkClient(smoke)
        clients.append(client)
        return client

    session = smoke.GoogleGenaiSmokeSession("synthetic-sdk-key", sdk_factory=sdk_factory)
    record = asyncio.run(smoke.run_contract_smoke(session, clock=lambda: 100.0))

    assert record["status"] == "passed"
    assert record["usage"] == {
        "indexed_bytes": len(smoke.synthetic_pdf_bytes()),
        "input_tokens": 13,
        "output_tokens": 8,
    }
    all_aio = [client.aio for client in clients]
    assert all(aio.closed == 1 for aio in all_aio)
    assert all_aio[0].file_search_stores.calls == [
        (
            "create",
            {
                "display_name": "Study Hub Task 2.8 synthetic contract",
                "embedding_model": "models/gemini-embedding-2",
            },
        )
    ]
    assert all_aio[1].files.calls[0][0] == "upload"
    assert all_aio[1].files.calls[0][1][1] == {
        "display_name": "task-2-8-synthetic.pdf",
        "mime_type": "application/pdf",
    }
    metadata = all_aio[2].file_search_stores.calls[0][1][2]["custom_metadata"]
    assert metadata[0] == {"key": "authority_class", "string_value": "course_material"}
    query_configs = [aio.models.calls[0][2] for aio in all_aio if aio.models.calls]
    assert len(query_configs) == 2
    assert all("thinking_config" not in config for config in query_configs)
    assert query_configs[0]["response_schema"] is smoke.SmokeAnswer
    assert query_configs[0]["tools"] == [
        {
            "file_search": {
                "file_search_store_names": ["fileSearchStores/sdk-store"],
                "metadata_filter": (
                    'course_id="task-2-8-synthetic-course" AND '
                    'exam_id="task-2-8-synthetic-exam" AND '
                    'lecture_id="task-2-8-synthetic-lecture"'
                ),
            }
        }
    ]
    assert all_aio[-3].file_search_stores.documents.calls == [
        (
            "delete",
            (
                "fileSearchStores/sdk-store/documents/sdk-document",
                {"force": True},
            ),
        )
    ]
    assert all_aio[-1].file_search_stores.calls == [
        ("delete", ("fileSearchStores/sdk-store", {"force": True}))
    ]


def test_authorized_entrypoint_reads_stored_key_once_without_retaining_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_smoke()
    secrets = _FakeSecrets("stored-synthetic-key")
    created: list[str] = []

    def session_factory(api_key: str) -> _FakeSession:
        created.append(api_key)
        return _FakeSession(smoke)

    monkeypatch.setenv("RUN_LIVE_GEMINI_TESTS", "1")
    record = asyncio.run(
        smoke.run_authorized_live_smoke(
            secret_store=secrets,
            session_factory=session_factory,
        )
    )

    assert secrets.calls == ["gemini-api-key"]
    assert created == ["stored-synthetic-key"]
    assert "stored-synthetic-key" not in json.dumps(record, sort_keys=True)


def test_authorized_entrypoint_fails_closed_when_stored_key_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_smoke()
    secrets = _FakeSecrets(None)
    monkeypatch.setenv("RUN_LIVE_GEMINI_TESTS", "1")

    with pytest.raises(smoke.LiveSmokeBlocked, match="stored Gemini credential is unavailable"):
        asyncio.run(smoke.run_authorized_live_smoke(secret_store=secrets))

    assert secrets.calls == ["gemini-api-key"]


def test_completed_operation_error_keeps_safe_provider_classification() -> None:
    smoke = _load_smoke()

    def sdk_factory(**kwargs: object) -> _SdkClient:
        del kwargs
        return _SdkClient(smoke, operation_error_status=429)

    session = smoke.GoogleGenaiSmokeSession("synthetic-sdk-key", sdk_factory=sdk_factory)

    with pytest.raises(smoke.GeminiProviderError) as raised:
        asyncio.run(session.wait_for_import("operations/sdk-operation"))

    assert type(raised.value).__name__ == "GeminiQuotaError"
    assert "429" not in str(raised.value)


def test_operation_poll_is_bounded_by_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_smoke()
    ticks = iter((0.0, 899.999))
    monkeypatch.setattr(smoke, "monotonic", lambda: next(ticks))

    def sdk_factory(**kwargs: object) -> _SdkClient:
        del kwargs
        return _SdkClient(smoke, operation_delay=0.02)

    session = smoke.GoogleGenaiSmokeSession("synthetic-sdk-key", sdk_factory=sdk_factory)

    with pytest.raises(smoke.SmokeTemporaryFailure, match="timed out"):
        asyncio.run(session.wait_for_import("operations/sdk-operation"))


def test_primary_provider_failure_wins_when_cleanup_also_fails() -> None:
    smoke = _load_smoke()
    evidence: dict[str, object] = {}

    class BodyAndCleanupFailure(_FakeSession):
        async def delete_file(self, file_name: str) -> None:
            self.calls.append(("delete_file", file_name))
            raise smoke.SmokeContractError("synthetic cleanup failure")

    session = BodyAndCleanupFailure(smoke, fail_import=True)

    with pytest.raises(smoke.SmokeTemporaryFailure, match="temporary"):
        asyncio.run(smoke.run_contract_smoke(session, failure_evidence=evidence))

    assert [name for name, _ in session.calls][-2:] == ["delete_file", "delete_store"]
    assert evidence["cleanup"] == {"attempted": 2, "status": "failed"}


def test_failed_smoke_emits_only_redacted_stage_error_and_cleanup_evidence() -> None:
    smoke = _load_smoke()
    evidence: dict[str, object] = {}

    class QueryFailure(_FakeSession):
        async def query(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            self.calls.append(("query", "private-query-payload"))
            raise smoke.GeminiProviderError(
                "Gemini provider request failed.",
                provider_status_code=400,
            )

    session = QueryFailure(smoke)
    with pytest.raises(smoke.GeminiProviderError) as raised:
        asyncio.run(smoke.run_contract_smoke(session, failure_evidence=evidence))

    record = smoke._failure_record(raised.value, evidence)
    assert record == {
        "schema_version": 1,
        "status": "failed",
        "failure_stage": "positive_query",
        "error_category": "provider",
        "provider_status_code": 400,
        "retryable": False,
        "resources_created": {
            "document": "confirmed",
            "file": "confirmed",
            "store": "confirmed",
        },
        "cleanup": {"attempted": 3, "status": "completed"},
    }
    encoded = json.dumps(record, sort_keys=True)
    assert "private-query-payload" not in encoded
    assert smoke.SYNTHETIC_FACT not in encoded
    for raw_identity in (session.store_name, session.file_name, session.document_name):
        assert raw_identity not in encoded


def test_live_cli_failure_prints_redacted_json_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_smoke()

    async def fail_live(*, failure_evidence: dict[str, object]) -> dict[str, object]:
        failure_evidence.update(
            {
                "failure_stage": "positive_query",
                "resources_created": {
                    "document": "confirmed",
                    "file": "confirmed",
                    "store": "confirmed",
                },
                "cleanup": {"attempted": 3, "status": "completed"},
            }
        )
        raise smoke.GeminiProviderError(
            "Gemini provider request failed.",
            provider_status_code=400,
        )

    monkeypatch.setattr(smoke, "run_authorized_live_smoke", fail_live)

    assert smoke.main(["--execute-live"]) == 1
    record = json.loads(capsys.readouterr().out)
    assert record["failure_stage"] == "positive_query"
    assert record["provider_status_code"] == 400
    assert record["cleanup"] == {"attempted": 3, "status": "completed"}


def test_malformed_structured_output_is_redacted_at_cli_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    smoke = _load_smoke()

    class MalformedAnswer(_FakeSession):
        async def query(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            return smoke.SmokeQueryResult(
                answer={"private-provider-body": "must-not-escape"},
                citations=(),
            )

    async def fail_live(*, failure_evidence: dict[str, object]) -> dict[str, object]:
        return await smoke.run_contract_smoke(
            MalformedAnswer(smoke),
            failure_evidence=failure_evidence,
        )

    monkeypatch.setattr(smoke, "run_authorized_live_smoke", fail_live)

    assert smoke.main(["--execute-live"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["error_category"] == "contract"
    assert output.err == ""
    assert "private-provider-body" not in output.out
    assert "must-not-escape" not in output.out


def test_response_loss_records_unknown_resource_and_cleanup_outcome() -> None:
    smoke = _load_smoke()
    evidence: dict[str, object] = {}

    class StoreResponseLoss(_FakeSession):
        async def create_store(self, display_name: str, embedding_model: str) -> str:
            del display_name, embedding_model
            raise smoke.translate_gemini_error(TimeoutError("private response loss"))

    with pytest.raises(smoke.GeminiProviderError):
        asyncio.run(
            smoke.run_contract_smoke(
                StoreResponseLoss(smoke),
                failure_evidence=evidence,
            )
        )

    assert evidence == {
        "failure_stage": "create_store",
        "resources_created": {
            "document": "not_started",
            "file": "not_started",
            "store": "unknown",
        },
        "cleanup": {"attempted": 0, "status": "unknown"},
    }


def _clock() -> Iterator[float]:
    yield 100.0
    yield 101.25


def test_offline_fake_proves_full_smoke_sequence_and_redacted_record() -> None:
    smoke = _load_smoke()
    session = _FakeSession(smoke)
    clock = _clock()

    record = asyncio.run(smoke.run_contract_smoke(session, clock=lambda: next(clock)))

    encoded = json.dumps(record, sort_keys=True)
    assert record["status"] == "passed"
    assert record["document_types"] == ["pdf"]
    assert record["citation"]["page_number"] == 1
    assert record["negative_scope_retrieved"] is False
    assert record["structured_output"] == {
        "schema": "SmokeAnswer",
        "validated": True,
        "answer_sha256": hashlib.sha256(smoke.SYNTHETIC_FACT.encode("utf-8")).hexdigest(),
    }
    assert smoke.SYNTHETIC_FACT not in encoded
    assert record["thinking_configuration"] == "omitted"
    assert record["duration_ms"] == 1250
    assert record["usage"] == {
        "indexed_bytes": len(smoke.synthetic_pdf_bytes()),
        "input_tokens": 11,
        "output_tokens": 7,
    }
    assert record["cleanup"] == {"attempted": 3, "status": "completed"}
    for raw_identity in (
        session.store_name,
        session.file_name,
        session.operation_name,
        session.document_name,
    ):
        assert raw_identity not in encoded
    assert [name for name, _ in session.calls] == [
        "create_store",
        "upload_pdf",
        "import_file",
        "wait_for_import",
        "query",
        "query",
        "list_documents",
        "delete_document",
        "delete_file",
        "delete_store",
    ]


def test_temporary_failure_fake_cleans_up_without_a_live_outage() -> None:
    smoke = _load_smoke()
    session = _FakeSession(smoke, fail_import=True)

    with pytest.raises(smoke.SmokeTemporaryFailure, match="temporary"):
        asyncio.run(smoke.run_contract_smoke(session))

    assert [name for name, _ in session.calls] == [
        "create_store",
        "upload_pdf",
        "import_file",
        "delete_file",
        "delete_store",
    ]


def test_temporary_failure_fixture_persists_retry_state_then_resumes() -> None:
    smoke = _load_smoke()

    record = smoke.run_temporary_failure_fixture()

    assert record == {
        "first_state": "retryable_failure",
        "retry_count": 1,
        "error_category": "transient",
        "backoff_seconds": 5,
        "resumed_state": "ready",
        "service_calls": 2,
    }


def test_plan_names_the_required_later_live_wiring_change() -> None:
    smoke = _load_smoke()

    plan = smoke._plan()

    assert "run_authorized_live_smoke" in plan["required_owner_action"]
    assert plan["calls_provider"] is False
    assert plan["reads_secrets"] is False


def test_opt_in_without_stored_credential_fails_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    smoke = _load_smoke()
    monkeypatch.setenv("RUN_LIVE_GEMINI_TESTS", "1")

    with pytest.raises(smoke.LiveSmokeBlocked) as raised:
        asyncio.run(smoke.run_authorized_live_smoke())

    assert "stored Gemini credential is unavailable" in str(raised.value)


def test_synthetic_pdf_is_deterministic_and_contains_no_private_source() -> None:
    smoke = _load_smoke()

    first = smoke.synthetic_pdf_bytes()
    second = smoke.synthetic_pdf_bytes()

    assert first == second
    assert first.startswith(b"%PDF-")
    assert b"Lecture 13" not in first


def test_gate_2b_record_is_explicitly_blocked_until_live_acceptance() -> None:
    record = json.loads(GATE_RECORD.read_text(encoding="utf-8"))

    assert record["schema_version"] == 1
    assert record["gate"] == "gate_2b_gemini_provider"
    assert record["state"] == "blocked"
    assert record["claim"] == "gate_not_open"
    assert record["assessed_input"]["sha"] == "37c1b6ac572ecdc4549f4688ab5868c4a9a85240"
    assert {item["id"] for item in record["blockers"]} == {
        "live_provider_authorization",
        "official_sdk_dependency_and_adapter",
        "private_lecture_shadow_authorization",
        "live_acceptance_and_independent_review",
    }
    assert all(item["status"] == "blocked" for item in record["live_verification"])
    assert record["review"]["gate_open_approval"] == "not_granted"
    assert "provider_document_id" not in json.dumps(record)


@pytest.mark.skipif(not LIVE_ENABLED, reason="live Gemini contract tests are opt-in")
def test_live_gemini_file_search_contract() -> None:
    smoke = _load_smoke()
    asyncio.run(smoke.run_authorized_live_smoke())
