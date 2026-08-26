from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

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
    assert record["structured_output"] == {"answer": smoke.SYNTHETIC_FACT, "supported": True}
    assert record["thinking_configuration"] == "omitted"
    assert record["duration_ms"] == 1250
    assert record["usage"] == {
        "indexed_bytes": len(smoke.synthetic_pdf_bytes()),
        "input_tokens": 11,
        "output_tokens": 7,
    }
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
