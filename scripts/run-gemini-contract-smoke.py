#!/usr/bin/env python3
"""Offline-tested orchestration for the explicitly authorized Task 2.8 live smoke."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from time import monotonic
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict
from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from oms_hub.indexing.service import IndexResult

SYNTHETIC_COURSE_ID = "task-2-8-synthetic-course"
SYNTHETIC_EXAM_ID = "task-2-8-synthetic-exam"
SYNTHETIC_LECTURE_ID = "task-2-8-synthetic-lecture"
SYNTHETIC_REVISION_ID = "sr_aaaaaaaaaaaaaaaaaaaaaaaaaa"
SYNTHETIC_FACT = "The Task 2.8 synthetic marker is cobalt-otter-28."
WRONG_LECTURE_ID = "task-2-8-wrong-lecture"


class SmokeContractError(RuntimeError):
    pass


class SmokeTemporaryFailure(RuntimeError):
    pass


class LiveSmokeBlocked(RuntimeError):
    pass


class SmokeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    supported: bool


@dataclass(frozen=True, slots=True)
class SmokeScope:
    course_id: str
    exam_id: str
    lecture_id: str


@dataclass(frozen=True, slots=True)
class SmokeCitation:
    document_name: str
    page_number: int | None
    excerpt: str


@dataclass(frozen=True, slots=True)
class SmokeQueryResult:
    answer: dict[str, object]
    citations: tuple[SmokeCitation, ...]
    input_tokens: int | None = None
    output_tokens: int | None = None


class SmokeSession(Protocol):
    async def create_store(self, display_name: str, embedding_model: str) -> str: ...

    async def upload_pdf(self, display_name: str, content: bytes) -> str: ...

    async def import_file(
        self,
        store_name: str,
        file_name: str,
        metadata: tuple[tuple[str, str], ...],
    ) -> str: ...

    async def wait_for_import(self, operation_name: str) -> str: ...

    async def query(
        self,
        store_name: str,
        prompt: str,
        scope: SmokeScope,
        *,
        response_schema: type[SmokeAnswer],
        omit_thinking: bool,
    ) -> SmokeQueryResult: ...

    async def list_documents(self, store_name: str) -> tuple[str, ...]: ...

    async def delete_document(self, document_name: str) -> None: ...

    async def delete_file(self, file_name: str) -> None: ...

    async def delete_store(self, store_name: str) -> None: ...


class _TemporaryRetryService:
    def __init__(self) -> None:
        self.calls = 0

    async def index_revision(self, source_revision_id: str) -> IndexResult:
        from oms_hub.indexing.models import IndexState
        from oms_hub.indexing.service import IndexResult
        from oms_hub.providers.gemini.errors import GeminiTransientError

        self.calls += 1
        if self.calls == 1:
            raise GeminiTransientError("synthetic temporary failure")
        return IndexResult(source_revision_id, IndexState.READY)


def synthetic_pdf_bytes() -> bytes:
    output = BytesIO()
    page = Canvas(output, pagesize=letter, invariant=1, pageCompression=0)
    page.setTitle("Task 2.8 synthetic Gemini contract fixture")
    page.drawString(72, 720, SYNTHETIC_FACT)
    page.save()
    return output.getvalue()


async def run_contract_smoke(
    session: SmokeSession,
    *,
    clock: Callable[[], float] = monotonic,
) -> dict[str, object]:
    pdf = synthetic_pdf_bytes()
    digest = hashlib.sha256(pdf).hexdigest()
    metadata = (
        ("authority_class", "course_material"),
        ("course_id", SYNTHETIC_COURSE_ID),
        ("exam_id", SYNTHETIC_EXAM_ID),
        ("lecture_id", SYNTHETIC_LECTURE_ID),
        ("source_revision_id", SYNTHETIC_REVISION_ID),
        ("input_key", "pdf"),
        ("input_kind", "pdf"),
        ("input_sha256", digest),
    )
    store_name: str | None = None
    file_name: str | None = None
    document_name: str | None = None
    started = clock()
    try:
        store_name = await session.create_store(
            "Study Hub Task 2.8 synthetic contract",
            "models/gemini-embedding-2",
        )
        file_name = await session.upload_pdf("task-2-8-synthetic.pdf", pdf)
        operation_name = await session.import_file(store_name, file_name, metadata)
        document_name = await session.wait_for_import(operation_name)
        positive = await session.query(
            store_name,
            f"Return the exact synthetic marker stated in the indexed PDF: {SYNTHETIC_FACT}",
            SmokeScope(SYNTHETIC_COURSE_ID, SYNTHETIC_EXAM_ID, SYNTHETIC_LECTURE_ID),
            response_schema=SmokeAnswer,
            omit_thinking=True,
        )
        answer = SmokeAnswer.model_validate(positive.answer)
        if answer.answer != SYNTHETIC_FACT or not answer.supported:
            raise SmokeContractError("structured output did not preserve the synthetic fact")
        if len(positive.citations) != 1:
            raise SmokeContractError("positive query did not return exactly one citation")
        citation = positive.citations[0]
        if (
            citation.document_name != document_name
            or citation.page_number != 1
            or SYNTHETIC_FACT not in citation.excerpt
        ):
            raise SmokeContractError("positive citation did not resolve to PDF page one")
        negative = await session.query(
            store_name,
            "Return the indexed synthetic marker.",
            SmokeScope(SYNTHETIC_COURSE_ID, SYNTHETIC_EXAM_ID, WRONG_LECTURE_ID),
            response_schema=SmokeAnswer,
            omit_thinking=True,
        )
        if negative.citations:
            raise SmokeContractError("wrong-lecture metadata filter retrieved the document")
        if await session.list_documents(store_name) != (document_name,):
            raise SmokeContractError("document listing did not round-trip the imported document")
        duration_ms = round((clock() - started) * 1000)
        return {
            "schema_version": 1,
            "status": "passed",
            "sdk_version": "2.14.0",
            "model": "gemini-3.7-flash",
            "embedding_model": "models/gemini-embedding-2",
            "source_revision_hash": hashlib.sha256(
                SYNTHETIC_REVISION_ID.encode("utf-8")
            ).hexdigest(),
            "document_types": ["pdf"],
            "page_count": 1,
            "operation_states": ["done"],
            "citation_resolution_rate": 1.0,
            "citation": {
                "page_number": citation.page_number,
                "excerpt_sha256": hashlib.sha256(citation.excerpt.encode("utf-8")).hexdigest(),
            },
            "negative_scope_retrieved": False,
            "structured_output": answer.model_dump(mode="json"),
            "thinking_configuration": "omitted",
            "duration_ms": duration_ms,
            "usage": {
                "indexed_bytes": len(pdf),
                "input_tokens": positive.input_tokens,
                "output_tokens": positive.output_tokens,
            },
            "provider_ids": {
                "store": _redacted_identity(store_name),
                "file": _redacted_identity(file_name),
                "operation": _redacted_identity(operation_name),
                "document": _redacted_identity(document_name),
            },
            "warnings": [],
        }
    finally:
        await _cleanup(session, document_name, file_name, store_name)


async def _cleanup(
    session: SmokeSession,
    document_name: str | None,
    file_name: str | None,
    store_name: str | None,
) -> None:
    failures: list[str] = []
    for method, value in (
        (session.delete_document, document_name),
        (session.delete_file, file_name),
        (session.delete_store, store_name),
    ):
        if value is None:
            continue
        try:
            await method(value)
        except Exception as error:  # noqa: PERF203 - cleanup must attempt every resource
            failures.append(type(error).__name__)
    if failures:
        raise SmokeContractError(f"provider cleanup failed: {', '.join(failures)}")


def _redacted_identity(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def run_temporary_failure_fixture() -> dict[str, object]:
    from oms_hub.db import Database
    from oms_hub.indexing.models import IndexJob, ProviderStore, StoreKey
    from oms_hub.indexing.repository import IndexRepository
    from oms_hub.indexing.worker import IndexWorker

    database = Database("sqlite://")
    database.create_schema()
    try:
        repository = IndexRepository(database)
        key = StoreKey.course(SYNTHETIC_COURSE_ID, SYNTHETIC_EXAM_ID)
        store = repository.create_store(
            ProviderStore(
                store_key=key,
                provider="gemini",
                provider_store_name="offlineFakeStores/task-2-8",
                embedding_model="models/gemini-embedding-2",
                authority_namespace=key.authority_namespace,
                course_id=key.course_id,
                exam_id=key.exam_id,
            )
        )
        job = repository.save_job(
            IndexJob(store_id=store.id, source_revision_id=SYNTHETIC_REVISION_ID)
        )
        clock = [datetime(2026, 8, 26, 12, 0, tzinfo=UTC)]
        service = _TemporaryRetryService()
        worker = IndexWorker(
            repository,
            service,
            worker_id="task-2-8-offline-retry",
            lease_seconds=60,
            now=lambda: clock[0],
        )

        worker.run_once()
        retry = repository.get_job(job.id)
        assert retry is not None and retry.next_attempt_at is not None
        next_attempt = datetime.fromisoformat(retry.next_attempt_at)
        backoff_seconds = round((next_attempt - clock[0]).total_seconds())
        clock[0] = next_attempt
        worker.run_once()
        resumed = repository.get_job(job.id)
        assert resumed is not None
        return {
            "first_state": retry.state.value,
            "retry_count": retry.retry_count,
            "error_category": retry.last_error_category,
            "backoff_seconds": backoff_seconds,
            "resumed_state": resumed.state.value,
            "service_calls": service.calls,
        }
    finally:
        database.close()


async def run_authorized_live_smoke() -> dict[str, object]:
    if os.getenv("RUN_LIVE_GEMINI_TESTS") != "1":
        raise LiveSmokeBlocked("RUN_LIVE_GEMINI_TESTS=1 is required for a live smoke")
    raise LiveSmokeBlocked(
        "live provider execution remains blocked until Connor authorizes it and Program Sol-0 "
        "integrates the pinned google-genai 2.14.0 dependency plus reviewed live session adapter"
    )


def _plan() -> dict[str, object]:
    return {
        "status": "blocked_before_live_provider",
        "calls_provider": False,
        "reads_secrets": False,
        "required_flag": "RUN_LIVE_GEMINI_TESTS=1",
        "required_command": "python scripts/run-gemini-contract-smoke.py --execute-live",
        "required_authorization": (
            "Connor must explicitly authorize one synthetic Gemini smoke, disposable provider "
            "create/query/delete operations, quota/cost, and approved secret-store access."
        ),
        "required_owner_action": (
            "Program Sol-0 must integrate google-genai==2.14.0; a later explicitly authorized "
            "Sol-2 change must implement a reviewed live SmokeSession and wire it into "
            "run_authorized_live_smoke. The current hard block remains until that code lands."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute_live:
        print(json.dumps(_plan(), indent=2, sort_keys=True))
        return 0
    try:
        record = asyncio.run(run_authorized_live_smoke())
    except LiveSmokeBlocked as error:
        parser.error(str(error))
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
