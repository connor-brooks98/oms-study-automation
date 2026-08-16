from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from oms_hub import app as app_module
from oms_hub.anki.domain import ApplyState, CurationStage, CurationState, PipelineContractVersion
from oms_hub.anki.provider_attempts import (
    ProviderAttemptBinding,
    ProviderAttemptEvent,
    ProviderAttemptIdentity,
    ProviderEventEvidence,
    begin_provider_call,
    bind_provider_attempts,
    emit_provider_event,
    provider_call_scope,
    replay_namespace_from_job_source,
)
from oms_hub.anki.rehearsal import capture as capture_module
from oms_hub.anki.rehearsal import process as process_module
from oms_hub.anki.rehearsal.capture import (
    CaptureAnkiCurationRepository,
    CaptureAuthorization,
    CaptureDenied,
    CaptureEmbeddingClient,
    CaptureSecretStore,
    CaptureStore,
    CaptureStructuredTextGenerator,
    CaptureStructuredTextService,
)
from oms_hub.anki.rehearsal.process import (
    ProcessRehearsal,
    RehearsalRequest,
    _verify_replay_completion,
)
from oms_hub.anki.rehearsal.structured import (
    ReplayStructuredTextGenerator,
    structured_request_key_from_hashes,
)
from oms_hub.anki.rehearsal.vectors import ReplayEmbeddingClient
from oms_hub.anki.repository import AnkiCurationRepository
from oms_hub.anki.semantic.voyage import VoyageEmbeddingError
from oms_hub.anki.worker import AnkiCurationWorker
from oms_hub.llm.domain import GeneratedText, GenerationOptions, ProviderName

_COMMIT = "a" * 40
_TREE = "b" * 40
_CAPSULE = "c" * 64
_JOB = "12345678-1234-5678-1234-567812345678"


def _identity() -> dict[str, object]:
    return {
        "replay_namespace": "capture-test",
        "stage": "card_prefilter",
        "replay_attempt": 1,
        "call_kind": "query_embedding",
        "batch_ordinal": 0,
        "batch_note_ids_sha256": "0" * 64,
        "subcall_ordinal": 0,
    }


def _authorization(path: Path) -> tuple[Path, str]:
    document = {
        "schema_version": 1,
        "candidate": {"commit": _COMMIT, "tree": _TREE},
        "capsule_manifest_sha256": _CAPSULE,
        "phase_b8": {"evidence_sha256": "d" * 64, "lineage_sha256": "e" * 64},
        "failed_job": {"id": _JOB},
        "replay_namespace": "capture-test",
        "structured": [
            {
                "provider": "openrouter",
                "model": "test-model",
                "endpoint": "https://openrouter.example/v1/chat",
                "max_output_tokens": 10,
                "max_input_bytes": 10_000,
                "max_reserved_microusd": 10,
                "input_microusd_per_million": 1,
                "output_microusd_per_million": 1,
            }
        ],
        "voyage": {
            "model": "voyage-4-large",
            "dimensions": 2,
            "endpoint": "https://api.voyageai.com/v1/embeddings",
            "max_reserved_microusd": 10,
        },
        "egress_pins": {"openrouter.example": ["203.0.113.1"], "api.voyageai.com": ["203.0.113.2"]},
        "maxima": {
            "structured_calls": 2,
            "embedding_batches": 2,
            "embedding_rows": 4,
            "embedding_input_bytes": 100,
            "output_tokens": 20,
            "total_reserved_microusd": 100,
        },
    }
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> CaptureAuthorization:
    manifest, digest = _authorization(path)
    return CaptureAuthorization.load(
        manifest,
        digest,
        commit=_COMMIT,
        tree=_TREE,
        capsule_sha256=_CAPSULE,
        failed_job_id=_JOB,
    )


def test_capture_authorization_and_secret_facade_fail_closed(tmp_path: Path) -> None:
    authorization = _load(tmp_path / "authorization.json")
    with pytest.raises(CaptureDenied, match="identity"):
        CaptureAuthorization.load(
            tmp_path / "authorization.json",
            authorization.sha256,
            commit="f" * 40,
            tree=_TREE,
            capsule_sha256=_CAPSULE,
            failed_job_id=_JOB,
        )
    native = SimpleNamespace(get=lambda key: "allowed" if key == "openrouter-api-key" else None)
    secrets = CaptureSecretStore(native, frozenset({"openrouter-api-key"}))
    assert secrets.get("openrouter-api-key") == "allowed"
    with pytest.raises(CaptureDenied, match="not authorized"):
        secrets.get("unapproved-key")
    with pytest.raises(CaptureDenied, match="disabled"):
        secrets.set("openrouter-api-key", "new")
    with pytest.raises(CaptureDenied, match="disabled"):
        secrets.delete("openrouter-api-key")


def test_capture_control_plane_is_server_authoritative_and_audited(tmp_path: Path) -> None:
    capability = "capture-capability-not-for-evidence-123456"

    def app_for(root: Path) -> tuple[FastAPI, CaptureStore, list[str]]:
        root.mkdir()
        store = CaptureStore(root / "private", _load(root / "authorization.json"))
        store.prepare()
        app = FastAPI()
        invoked: list[str] = []

        @app.get("/health")
        def health() -> dict[str, str]:
            invoked.append("health")
            return {"status": "ok"}

        @app.post("/api/anki/jobs", status_code=201)
        def create() -> dict[str, str]:
            invoked.append("create")
            return {"id": str(UUID(int=71))}

        @app.get("/api/anki/jobs/{job_id}")
        def status(job_id: UUID) -> dict[str, str]:
            invoked.append(f"status:{job_id}")
            return {"state": "ready_for_review"}

        @app.post("/api/settings/providers/test")
        def provider_test() -> dict[str, str]:
            invoked.append("provider-test")
            return {"status": "should-not-run"}

        app_module._install_capture_control_plane(app, store, capability)
        return app, store, invoked

    denied_app, denied_store, denied_invoked = app_for(tmp_path / "denied")
    denied = TestClient(denied_app)
    assert denied.get("/health").status_code == 401
    assert denied.get("/health", headers={"X-OMS-Capture-Capability": "wrong"}).status_code == 401
    headers = {"X-OMS-Capture-Capability": capability}
    for method, path in (
        ("post", "/api/settings/providers/test"),
        ("post", "/api/anki/jobs/not-a-uuid/apply"),
        ("get", "/static/anki.js"),
        ("get", "/api/anki/jobs/not-a-uuid"),
        ("get", "/health?x=1"),
        ("post", "/api/anki/jobs?x=1"),
    ):
        assert getattr(denied, method)(path, headers=headers).status_code == 404
    assert denied_invoked == []
    assert any(entry["authenticated"] is False for entry in denied_store.server_audit()["entries"])

    app, store, invoked = app_for(tmp_path / "allowed")
    client = TestClient(app)
    assert client.get("/health", headers=headers).status_code == 200
    first = client.post("/api/anki/jobs", headers=headers)
    assert first.status_code == 201
    job_id = first.json()["id"]
    assert client.get(f"/api/anki/jobs/{job_id}", headers=headers).status_code == 200
    assert client.get(f"/api/anki/jobs/{job_id}?x=1", headers=headers).status_code == 404
    assert client.post("/api/anki/jobs", headers=headers).status_code == 409
    assert invoked == ["health", "create", f"status:{job_id}"]
    audit = store.server_audit()
    assert capability not in json.dumps(audit)
    assert [entry["ordinal"] for entry in audit["entries"]] == [1, 2, 3, 4, 5]
    assert audit["entries"][1]["job_id"] == job_id
    assert audit["entries"][2]["job_id"] == job_id


def test_capture_control_plane_audits_invalid_asgi_paths_with_safe_sentinels(
    tmp_path: Path,
) -> None:
    capability = "capture-capability-not-for-evidence-123456"
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    app = FastAPI()
    app_module._install_capture_control_plane(app, store, capability)

    async def request(
        raw_path: object, path: object, query: object = b"", *, include_query: bool = True
    ) -> list[dict[str, object]]:
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        scope: dict[str, object] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": raw_path,
            "headers": [(b"x-oms-capture-capability", capability.encode())],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
        }
        if include_query:
            scope["query_string"] = query
        await app(scope, receive, send)
        return sent

    assert asyncio.run(request(b"\xff", "\x00"))[0]["status"] == 404
    assert asyncio.run(request(b"/%2fapi%2fanki%2fjobs", "/api/anki/jobs"))[0]["status"] == 404
    assert asyncio.run(request(b"/health", "/health", b"\xff"))[0]["status"] == 404
    assert asyncio.run(request(b"/health", "/health", "not-bytes"))[0]["status"] == 404
    assert asyncio.run(request(b"/health", "/health", include_query=False))[0]["status"] == 404
    entries = store.server_audit()["entries"]
    assert entries[0]["raw_path"] == "<invalid-raw-path>"
    assert entries[0]["canonical_path"] == "<invalid-canonical-path>"
    assert entries[1]["raw_path"] == "/%2fapi%2fanki%2fjobs"
    assert entries[1]["allowed"] is False
    assert entries[2]["query_state"] == "<invalid-query-string>"
    assert entries[3]["query_state"] == "<invalid-query-string>"
    assert entries[4]["query_state"] == "<invalid-query-string>"


def test_capture_audit_append_failure_poison_survives_storage_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability = "capture-capability-not-for-evidence-123456"
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    app = FastAPI()
    app_module._install_capture_control_plane(app, store, capability)
    original = store.record_server_request
    failed = False

    def fail_once(**kwargs: object) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise CaptureDenied("injected audit write failure")
        original(**kwargs)

    monkeypatch.setattr(store, "record_server_request", fail_once)
    client = TestClient(app)
    assert client.get("/review").status_code == 500
    monkeypatch.setattr(store, "record_server_request", original)
    response = client.get("/health", headers={"X-OMS-Capture-Capability": capability})
    assert response.status_code == 500
    with pytest.raises(CaptureDenied, match="poisoned"):
        store.server_audit()


def test_capture_server_audit_mismatch_or_denial_prevents_completion(tmp_path: Path) -> None:
    harness = ProcessRehearsal(_capture_request(tmp_path))
    assert harness._capture_capability is not None  # noqa: SLF001 - capture binding seam
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    store.initialize_server_audit(harness._capture_capability)  # noqa: SLF001
    job_id = str(UUID(int=72))
    expected = [
        ("GET", "/health", 200, None),
        ("POST", "/api/anki/jobs", 201, job_id),
        ("GET", f"/api/anki/jobs/{job_id}", 200, job_id),
    ]
    for method, path, status, bound_job in expected:
        store.record_server_request(
            method=method,
            raw_path=path,
            canonical_path=path,
            authenticated=True,
            allowed=True,
            status=status,
            job_id=bound_job,
            query_state="empty",
        )
    harness._capture_store = store
    client = SimpleNamespace(
        transcript=[
            {"method": method, "path": path, "status": status}
            for method, path, status, _bound_job in expected
        ]
    )
    assert harness._assert_capture_server_audit(client, UUID(job_id))["entries"]
    original_audit = store.server_audit()
    swapped_audit = json.loads(json.dumps(original_audit))
    swapped_audit["capability_sha256"] = "0" * 64
    store._write_json(store.root / "capture-server-audit.json", swapped_audit)  # noqa: SLF001
    with pytest.raises(RuntimeError, match="capability binding"):
        harness._assert_capture_server_audit(client, UUID(job_id))
    store._write_json(store.root / "capture-server-audit.json", original_audit)  # noqa: SLF001
    client.transcript[-1]["path"] = "/api/anki/jobs/00000000-0000-0000-0000-000000000000"
    with pytest.raises(RuntimeError, match="does not cover"):
        harness._assert_capture_server_audit(client, UUID(job_id))
    client.transcript[-1]["path"] = f"/api/anki/jobs/{job_id}"
    store.record_server_request(
        method="GET",
        raw_path="/review",
        canonical_path="/review",
        authenticated=True,
        allowed=False,
        status=404,
        job_id=None,
        query_state="empty",
    )
    with pytest.raises(RuntimeError, match="denied"):
        harness._assert_capture_server_audit(client, UUID(job_id))


@pytest.mark.parametrize(
    "job",
    (
        SimpleNamespace(
            state=CurationState.READY_FOR_REVIEW,
            review_revision=1,
            apply_state=ApplyState.PENDING,
        ),
        SimpleNamespace(
            state=CurationState.READY_FOR_REVIEW,
            review_revision=0,
            apply_state=ApplyState.COMPLETE,
        ),
        SimpleNamespace(
            state=CurationState.FAILED,
            review_revision=0,
            apply_state=ApplyState.PENDING,
        ),
    ),
)
def test_capture_completion_rejects_non_pristine_persisted_job_state(
    job: object, tmp_path: Path
) -> None:
    harness = ProcessRehearsal(_capture_request(tmp_path))
    repository = SimpleNamespace(require_job=lambda _job: job)
    with pytest.raises(RuntimeError, match="review, envelope, apply"):
        harness._validate_capture_ready_for_review_state(repository, UUID(int=73))


def test_capture_store_reserves_before_dispatch_and_rejects_collisions(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    assert store.root.stat().st_mode & 0o777 == 0o700
    first = store.reserve(kind="structured", output_tokens=10, replay_identity=_identity())
    with pytest.raises(CaptureDenied, match="budget"):
        store.reserve(kind="structured", output_tokens=11, replay_identity=_identity())
    generated = GeneratedText(
        text='{"ok":true}',
        provider=ProviderName.OPENROUTER,
        model="test-model",
        request_id="request-1",
        input_tokens=1,
        output_tokens=1,
        cost_microusd=0,
    )
    store.record_structured("key", generated)
    store.complete(first, observed_microusd=0, stored=True)
    changed = GeneratedText(
        text='{"ok":false}',
        provider=ProviderName.OPENROUTER,
        model="test-model",
        request_id="request-1",
        input_tokens=1,
        output_tokens=1,
        cost_microusd=0,
    )
    with pytest.raises(CaptureDenied, match="different response"):
        store.record_structured("key", changed)
    store.record_vectors(
        ["query"],
        np.asarray([[1.0, 0.0]], dtype=np.float32),
        model="voyage-4-large",
        dimensions=2,
        input_type="query",
    )
    with pytest.raises(CaptureDenied, match="different bytes"):
        store.record_vectors(
            ["query"],
            np.asarray([[0.0, 1.0]], dtype=np.float32),
            model="voyage-4-large",
            dimensions=2,
            input_type="query",
        )
    store.reserve(kind="query_embedding", rows=1, input_bytes=5, replay_identity=_identity())
    with pytest.raises(CaptureDenied, match="incomplete"):
        store.finalize_pack()


def test_capture_private_response_binding_detects_tampered_structured_and_vectors(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    structured_parameters = {
        "thinking": "disabled",
        "thinking_budget_tokens": 1024,
        "temperature": None,
        "max_tokens": None,
    }
    structured_identity = _identity()
    structured_request = {
        "kind": "structured",
        "provider": "openrouter",
        "model": "test-model",
        "instruction_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "output_schema_sha256": "c" * 64,
        "cache_prefix_sha256": None,
        "generation_parameters": structured_parameters,
        "replay_identity": structured_identity,
    }
    structured_request["key"] = structured_request_key_from_hashes(
        provider="openrouter",
        model="test-model",
        instruction_sha256="a" * 64,
        input_sha256="b" * 64,
        output_schema_sha256="c" * 64,
        cache_prefix_sha256=None,
        generation_parameters=structured_parameters,
        attempt_identity=structured_identity,
    )
    structured = store.reserve(
        kind="structured",
        output_tokens=1,
        provider="openrouter",
        model="test-model",
        replay_identity=_identity(),
        replay_request=structured_request,
    )
    generated = GeneratedText(
        '{"answer":"ok"}', ProviderName.OPENROUTER, "test-model", "request", 0, 0, 0
    )
    store.record_structured(str(structured_request["key"]), generated)
    structured_digest = capture_module._provider_response_sha256(generated.text)
    store.bind_private_response(
        structured, structured_digest, structured_request
    )
    structured_event = {
        "response_sha256": structured_digest,
        "request_sha256": "0" * 64,
        "provider": "openrouter",
        "model": "test-model",
        "instruction_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "output_schema_sha256": "c" * 64,
        "cache_prefix_sha256": None,
        "generation_parameters": {
            "thinking": "disabled",
            "thinking_budget_tokens": 1024,
            "temperature": None,
            "max_tokens": 10,
        },
        "request_id": "request",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_microusd": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    assert store.private_response_matches(store.calls()[0], structured_event)
    ledger = json.loads((store.root / "capture-ledger.json").read_text(encoding="utf-8"))
    ledger["calls"][0]["replay_request"]["key"] = "0" * 64
    ledger["calls"][0]["private_response"]["key"] = "0" * 64
    store._write_json(store.root / "capture-ledger.json", ledger)  # noqa: SLF001 - private test seam
    assert not store.private_response_matches(store.calls()[0], structured_event)
    ledger["calls"][0]["replay_request"] = structured_request
    ledger["calls"][0]["private_response"] = structured_request
    store._write_json(store.root / "capture-ledger.json", ledger)  # noqa: SLF001 - private test seam
    records = json.loads((store.pack / "structured.json").read_text(encoding="utf-8"))
    records[str(structured_request["key"])]["text"] = '{"answer":"tampered"}'
    store._write_json(store.pack / "structured.json", records)  # noqa: SLF001 - private test seam
    assert not store.private_response_matches(store.calls()[0], structured_event)
    records[str(structured_request["key"])]["text"] = generated.text
    records[str(structured_request["key"])]["provider"] = "openai"
    store._write_json(store.pack / "structured.json", records)  # noqa: SLF001 - private test seam
    assert not store.private_response_matches(store.calls()[0], structured_event)
    records[str(structured_request["key"])]["provider"] = "openrouter"
    records[str(structured_request["key"])]["model"] = "wrong-model"
    store._write_json(store.pack / "structured.json", records)  # noqa: SLF001 - private test seam
    assert not store.private_response_matches(store.calls()[0], structured_event)

    vector_key = ReplayEmbeddingClient(
        store.pack / "vectors", model="voyage-4-large", dimensions=2
    )._key("query", "query")  # noqa: SLF001 - published replay identity
    normalized_hashes = [hashlib.sha256(b"query").hexdigest()]
    provider_input_sha256 = hashlib.sha256(
        json.dumps(normalized_hashes, separators=(",", ":")).encode()
    ).hexdigest()
    provider_generation_parameters_sha256 = hashlib.sha256(
        json.dumps(
            {"input_type": "query", "row_count": 1},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    vector_request = {
        "kind": "vectors",
        "normalized_texts": ["query"],
        "keys": [vector_key],
        "text_sha256": normalized_hashes,
        "input_type": "query",
        "dimensions": 2,
        "provider_input_sha256": provider_input_sha256,
        "provider_generation_parameters_sha256": provider_generation_parameters_sha256,
    }

    vectors = store.reserve(
        kind="query_embedding",
        rows=1,
        input_bytes=5,
        provider="voyage",
        model="voyage-4-large",
        replay_identity=_identity(),
        replay_request=vector_request,
    )
    keys = store.record_vectors(
        ["query"],
        np.asarray([[1.0, 0.0]], dtype=np.float32),
        model="voyage-4-large",
        dimensions=2,
        input_type="query",
    )
    vector_response = json.dumps(
        [hashlib.sha256(np.asarray([1.0, 0.0], dtype=np.float32).tobytes()).hexdigest()],
        separators=(",", ":"),
    )
    vector_digest = capture_module._provider_response_sha256(vector_response)
    store.bind_private_response(
        vectors,
        vector_digest,
        vector_request,
    )
    assert keys == [vector_key]
    vector_event = {
        "response_sha256": vector_digest,
        "input_sha256": provider_input_sha256,
        "generation_parameters_sha256": provider_generation_parameters_sha256,
    }
    assert store.private_response_matches(store.calls()[1], vector_event)
    ledger = json.loads((store.root / "capture-ledger.json").read_text(encoding="utf-8"))
    ledger["calls"][1]["replay_request"]["normalized_texts"] = ["rewritten"]
    ledger["calls"][1]["private_response"]["normalized_texts"] = ["rewritten"]
    store._write_json(store.root / "capture-ledger.json", ledger)  # noqa: SLF001 - private test seam
    assert not store.private_response_matches(store.calls()[1], vector_event)
    ledger["calls"][1]["replay_request"] = vector_request
    ledger["calls"][1]["private_response"] = vector_request
    store._write_json(store.root / "capture-ledger.json", ledger)  # noqa: SLF001 - private test seam
    manifest_path = store.pack / "vectors" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[keys[0]]["sha256"] = "0" * 64
    store._write_json(manifest_path, manifest)  # noqa: SLF001 - private test seam
    assert not store.private_response_matches(store.calls()[1], vector_event)
    manifest[keys[0]]["sha256"] = hashlib.sha256(
        (store.pack / "vectors" / "query" / f"{keys[0]}.npy").read_bytes()
    ).hexdigest()
    manifest[keys[0]]["path"] = "document/other.npy"
    store._write_json(manifest_path, manifest)  # noqa: SLF001 - private test seam
    assert not store.private_response_matches(store.calls()[1], vector_event)
    manifest[keys[0]]["path"] = f"query/{keys[0]}.npy"
    manifest[keys[0]]["input_type"] = "document"
    store._write_json(manifest_path, manifest)  # noqa: SLF001 - private test seam
    assert not store.private_response_matches(store.calls()[1], vector_event)
    vector_path = store.pack / "vectors" / "query" / f"{keys[0]}.npy"
    store._write_bytes(vector_path, b"tampered")  # noqa: SLF001 - private test seam
    assert not store.private_response_matches(store.calls()[1], vector_event)


def test_capture_wrappers_write_replay_compatible_structured_and_vectors(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()

    class Structured:
        def generate_text(self, *_args: object, **_kwargs: object) -> GeneratedText:
            return GeneratedText(
                text='{"ok":true}',
                provider=ProviderName.OPENROUTER,
                model="test-model",
                request_id="request-1",
                input_tokens=1,
                output_tokens=0,
                cost_microusd=0,
            )

    structured = CaptureStructuredTextGenerator(
        Structured(), store, {ProviderName.OPENROUTER: "https://openrouter.example/v1/chat"}
    )
    binding = ProviderAttemptBinding(
        job_id=UUID(int=1),
        stage=CurationStage.CARD_FAST_CLASSIFY,
        stage_attempt=1,
        mode="shadow",
        recorder=lambda _event: None,
        replay_namespace="capture-test",
    )
    with bind_provider_attempts(binding), provider_call_scope(batch_index=0):
        handle = begin_provider_call(
            provider="openrouter",
            model="test-model",
            instruction="instruction",
            input_text="input",
            output_schema={"type": "object"},
            generation_parameters={
                "thinking": "disabled",
                "thinking_budget_tokens": 1024,
                "temperature": None,
                "max_tokens": 1,
            },
            cacheable_source_prefix=None,
        )
        emit_provider_event(handle, "dispatched")
        generated = structured.generate_text(
            "instruction",
            "input",
            output_schema={"type": "object"},
            provider=ProviderName.OPENROUTER,
            model="test-model",
            options=GenerationOptions(max_tokens=1),
        )
        emit_provider_event(
            handle, "response_received", request_id="request-1", response_text='{"ok":true}'
        )
        emit_provider_event(handle, "accepted", request_id="request-1")
    assert generated.request_id == "request-1"

    class Live:
        model = "voyage-4-large"
        dimensions = 2
        url = "https://api.voyageai.com/v1/embeddings"

        received: list[str] = []

        async def embed(self, texts: list[str], *, input_type: str) -> np.ndarray:
            assert input_type == "query"
            self.received = texts
            return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)

        async def aclose(self) -> None:
            return None

    live = Live()
    vectors = CaptureEmbeddingClient(live, store)  # type: ignore[arg-type]
    vector_binding = ProviderAttemptBinding(
        job_id=UUID(int=1),
        stage=CurationStage.CARD_PREFILTER,
        stage_attempt=1,
        mode="shadow",
        recorder=lambda _event: None,
        replay_namespace="capture-test",
    )
    with (
        bind_provider_attempts(vector_binding),
        provider_call_scope(batch_index=0, kind="query_embedding"),
    ):
        matrix = asyncio.run(vectors.embed(["query  with   spaces"], input_type="query"))
    assert matrix.shape == (1, 2)
    assert live.received == ["query with spaces"]
    pack = store.finalize_pack()
    replay = ReplayStructuredTextGenerator(
        store.pack / "structured.json", require_attempt_identity=True
    )
    replay_binding = ProviderAttemptBinding(
        job_id=UUID(int=2),
        stage=CurationStage.CARD_FAST_CLASSIFY,
        stage_attempt=9,
        mode="canonical",
        recorder=lambda _event: None,
        replay_namespace="capture-test",
    )
    with bind_provider_attempts(replay_binding), provider_call_scope(batch_index=0):
        replay_handle = begin_provider_call(
            provider="openrouter",
            model="test-model",
            instruction="instruction",
            input_text="input",
            output_schema={"type": "object"},
            generation_parameters={
                "thinking": "disabled",
                "thinking_budget_tokens": 1024,
                "temperature": None,
                "max_tokens": 1,
            },
            cacheable_source_prefix=None,
        )
        assert replay_handle is not None
        assert (
            replay.generate_text(
                "instruction",
                "input",
                output_schema={"type": "object"},
                provider=ProviderName.OPENROUTER,
                model="test-model",
                options=GenerationOptions(max_tokens=1),
            ).text
            == '{"ok":true}'
        )
    replay_vectors = ReplayEmbeddingClient(
        store.pack / "vectors", model="voyage-4-large", dimensions=2
    )
    assert asyncio.run(
        replay_vectors.embed(["query with spaces"], input_type="query")
    ).shape == (1, 2)
    assert len(pack["pack_manifest_sha256"]) == 64


def test_capture_goal_rejects_restart_failure_injection_and_forbidden_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    implementation = tmp_path / "implementation"
    implementation.mkdir()
    (capsule / "capsule.json").write_text("{}", encoding="utf-8")
    request = RehearsalRequest(
        capsule=capsule,
        overlay=tmp_path / "overlay",
        mode="shadow",
        port=8788,
        evidence_zip=tmp_path / "evidence.zip",
        failed_job_id=UUID(_JOB),
        expected_manifest_sha256=hashlib.sha256(
            (capsule / "capsule.json").read_bytes()
        ).hexdigest(),
        implementation_repository=implementation,
        expected_implementation_commit=_COMMIT,
        expected_implementation_tree=_TREE,
        trusted_python=Path("/bin/sh"),
        shadow_egress_pins_json="{}",
        run_goal="capture",
        restart_after_durable_boundary=True,
    )
    monkeypatch.setattr(ProcessRehearsal, "_verify_implementation_identity", lambda self: None)
    monkeypatch.setattr(process_module, "verify_capsule", lambda _path: SimpleNamespace())
    with pytest.raises(ValueError, match="disable restart"):
        ProcessRehearsal(request)._validate_destinations()
    harness = ProcessRehearsal(request)
    job = UUID(int=2)
    with pytest.raises(RuntimeError, match="forbidden"):
        harness._assert_capture_http_transcript(
            SimpleNamespace(transcript=[{"method": "POST", "path": f"/api/anki/jobs/{job}/apply"}]),
            job,
        )


def test_windows_acl_parser_and_command_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert capture_module._windows_acl_is_owner_only("DOMAIN\\owner:(F)\n", "DOMAIN\\owner")
    assert not capture_module._windows_acl_is_owner_only(
        "DOMAIN\\owner:(F)\nBUILTIN\\Users:(RX)\n", "DOMAIN\\owner"
    )
    assert not capture_module._windows_acl_is_owner_only("DOMAIN\\owner:(I)(F)\n", "DOMAIN\\owner")
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.root.mkdir()
    monkeypatch.setattr(capture_module, "_windows_current_principal", lambda: "DOMAIN\\owner")
    calls: list[list[str]] = []
    replies = iter(
        [
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(
                returncode=0,
                stdout=(
                    f"{store.root} OWNER RIGHTS:(F)\n"
                    "SYSTEM:(F)\n"
                    "BUILTIN\\Administrators:(F)\n"
                    "DOMAIN\\owner:(F)\n"
                ),
            ),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout="DOMAIN\\owner:(F)\n"),
        ]
    )

    def run(arguments: list[str], **_kwargs: object) -> object:
        calls.append(arguments)
        return next(replies)

    monkeypatch.setattr(capture_module.subprocess, "run", run)
    store._lock_windows_acl()
    assert calls == [
        ["icacls", str(store.root), "/inheritance:r"],
        ["icacls", str(store.root)],
        ["icacls", str(store.root), "/remove", "BUILTIN\\Administrators"],
        ["icacls", str(store.root), "/remove", "OWNER RIGHTS"],
        ["icacls", str(store.root), "/remove", "SYSTEM"],
        ["icacls", str(store.root), "/grant:r", "DOMAIN\\owner:(OI)(CI)F"],
        ["icacls", str(store.root)],
    ]
    monkeypatch.setattr(
        capture_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    with pytest.raises(CaptureDenied, match="created"):
        store._lock_windows_acl()
    replies = iter(
        [
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout="DOMAIN\\owner:(F)\n"),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=1, stdout=""),
        ]
    )
    monkeypatch.setattr(capture_module.subprocess, "run", lambda *_args, **_kwargs: next(replies))
    with pytest.raises(CaptureDenied, match="proven"):
        store._lock_windows_acl()


def test_windows_write_through_replace_is_required_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str, int]] = []
    fake_ctypes = SimpleNamespace(
        windll=SimpleNamespace(
            kernel32=SimpleNamespace(
                MoveFileExW=lambda source, destination, flags: (
                    calls.append((source, destination, flags)) or 1
                )
            )
        ),
        get_last_error=lambda: 5,
    )
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"payload")
    capture_module._move_file_ex_write_through(source, destination)
    assert calls == [(str(source), str(destination), 0x1 | 0x8)]
    assert source.exists()  # mocked native primitive made no durability claim
    failing_ctypes = SimpleNamespace(
        windll=SimpleNamespace(kernel32=SimpleNamespace(MoveFileExW=lambda *_args: 0)),
        get_last_error=lambda: 5,
    )
    monkeypatch.setitem(sys.modules, "ctypes", failing_ctypes)
    with pytest.raises(CaptureDenied, match="write-through"):
        capture_module._move_file_ex_write_through(source, destination)
    assert source.exists() and not destination.exists()


def test_capture_store_concurrent_transactions_are_lossless(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    barrier = threading.Barrier(3)
    ordinals: list[int] = []

    def reserve(index: int) -> None:
        barrier.wait()
        ordinal = store.reserve(
            kind="structured",
            output_tokens=1,
            provider="openrouter",
            model="test-model",
            request_sha256=f"{index + 1:x}" * 64,
            replay_identity=_identity(),
        )
        ordinals.append(ordinal)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(ordinals) == [1, 2]
    store.complete(2, observed_microusd=0, stored=True)
    store.complete(1, observed_microusd=0, stored=True)
    write_barrier = threading.Barrier(3)

    def write(index: int) -> None:
        write_barrier.wait()
        store.record_structured(
            f"key-{index}",
            GeneratedText(
                text=f'{{"index":{index}}}',
                provider=ProviderName.OPENROUTER,
                model="test-model",
                request_id=f"request-{index}",
                input_tokens=0,
                output_tokens=0,
                cost_microusd=0,
            ),
        )

    writers = [threading.Thread(target=write, args=(index,)) for index in range(2)]
    for thread in writers:
        thread.start()
    write_barrier.wait()
    for thread in writers:
        thread.join()
    assert set(json.loads((store.pack / "structured.json").read_text())) == {"key-0", "key-1"}
    assert [call["ordinal"] for call in store.calls()] == [1, 2]


def test_capture_transcript_rejects_every_review_mutation_route(tmp_path: Path) -> None:
    harness = ProcessRehearsal(_capture_request(tmp_path))
    job = UUID(int=3)
    for method, path in (
        ("GET", f"/api/anki/jobs/{job}/review"),
        ("PUT", f"/api/anki/jobs/{job}/review"),
        ("POST", f"/api/anki/jobs/{job}/envelope"),
        ("POST", f"/api/anki/jobs/{job}/apply"),
    ):
        with pytest.raises(RuntimeError, match="forbidden"):
            harness._assert_capture_http_transcript(
                SimpleNamespace(transcript=[{"method": method, "path": path, "status": 200}]), job
            )


def _capture_request(tmp_path: Path, **changes: object) -> RehearsalRequest:
    capsule = tmp_path / "capsule"
    capsule.mkdir(exist_ok=True)
    implementation = tmp_path / "implementation"
    implementation.mkdir(exist_ok=True)
    (capsule / "capsule.json").write_text("{}", encoding="utf-8")
    authorization, digest = _authorization(tmp_path / "authorization.json")
    authorization_document = json.loads(authorization.read_text(encoding="utf-8"))
    authorization_document["capsule_manifest_sha256"] = hashlib.sha256(
        (capsule / "capsule.json").read_bytes()
    ).hexdigest()
    authorization.write_text(json.dumps(authorization_document, sort_keys=True), encoding="utf-8")
    digest = hashlib.sha256(authorization.read_bytes()).hexdigest()
    values: dict[str, object] = {
        "capsule": capsule,
        "overlay": tmp_path / "overlay",
        "mode": "shadow",
        "port": 8788,
        "evidence_zip": tmp_path / "evidence.zip",
        "failed_job_id": UUID(_JOB),
        "expected_manifest_sha256": hashlib.sha256(
            (capsule / "capsule.json").read_bytes()
        ).hexdigest(),
        "implementation_repository": implementation,
        "expected_implementation_commit": _COMMIT,
        "expected_implementation_tree": _TREE,
        "trusted_python": Path("/bin/sh"),
        "shadow_egress_pins_json": json.dumps(json.loads(authorization.read_text())["egress_pins"]),
        "run_goal": "capture",
        "restart_after_durable_boundary": False,
        "capture_store": tmp_path / "private",
        "capture_authorization_manifest": authorization,
        "expected_capture_authorization_manifest_sha256": digest,
    }
    values.update(changes)
    return RehearsalRequest(**values)  # type: ignore[arg-type]


def test_capture_request_validation_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProcessRehearsal, "_verify_implementation_identity", lambda self: None)
    monkeypatch.setattr(process_module, "verify_capsule", lambda _path: SimpleNamespace())
    cases = (
        ({"mode": "deterministic", "shadow_egress_pins_json": None}, "deterministic rehearsal"),
        ({"restart_after_durable_boundary": True}, "disable restart"),
        ({"failure_injection": (CurationStage.CARD_RESIDUAL, "begun")}, "failure injection"),
        ({"capture_store": None}, "private store"),
        ({"expected_capture_authorization_manifest_sha256": "0" * 64}, "SHA-256"),
        ({"shadow_egress_pins_json": "{}"}, "egress pins"),
        (
            {
                "replay_supplement": tmp_path / "missing-replay",
                "expected_replay_supplement_manifest_sha256": "0" * 64,
            },
            "directory is unavailable",
        ),
    )
    for index, (changes, message) in enumerate(cases):
        root = tmp_path / str(index)
        root.mkdir()
        with pytest.raises((ValueError, CaptureDenied), match=message):
            ProcessRehearsal(_capture_request(root, **changes))._validate_destinations()
    root = tmp_path / "existing"
    root.mkdir()
    request = _capture_request(root)
    assert request.capture_store is not None
    request.capture_store.mkdir()
    with pytest.raises(ValueError, match="absent"):
        ProcessRehearsal(request)._validate_destinations()
    root = tmp_path / "identity"
    root.mkdir()
    request = _capture_request(root)
    assert request.capture_authorization_manifest is not None
    document = json.loads(request.capture_authorization_manifest.read_text())
    document["candidate"]["commit"] = "f" * 40
    request.capture_authorization_manifest.write_text(json.dumps(document), encoding="utf-8")
    request = replace(
        request,
        expected_capture_authorization_manifest_sha256=hashlib.sha256(
            request.capture_authorization_manifest.read_bytes()
        ).hexdigest(),
    )
    with pytest.raises(CaptureDenied, match="identity"):
        ProcessRehearsal(request)._validate_destinations()


def test_capture_reconciliation_and_namespace_mismatch_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def store_for(root: Path) -> CaptureStore:
        store = CaptureStore(root / "private", _load(root / "authorization.json"))
        store.prepare()
        ordinal = store.reserve(
            kind="structured",
            provider="openrouter",
            model="test-model",
            request_sha256="a" * 64,
            replay_identity=_identity(),
        )
        store.complete(ordinal, observed_microusd=0, stored=True)
        return store

    base = {
        "provider": "openrouter",
        "model": "test-model",
        "request_sha256": "a" * 64,
        "stage": "card_prefilter",
        "kind": "primary",
        "batch_index": 0,
        "batch_note_ids_sha256": "0" * 64,
        "subcall_ordinal": 0,
    }
    for index, events in enumerate(
        (
            [],
            [base | {"event": "dispatched"}],
            [
                base | {"event": "dispatched"},
                base | {"event": "accepted", "request_sha256": "b" * 64},
            ],
        )
    ):
        root = tmp_path / str(index)
        root.mkdir()
        harness = ProcessRehearsal(_capture_request(root))
        harness._capture_store = store_for(root)
        with pytest.raises(RuntimeError):
            harness._reconcile_capture_calls(events)
    root = tmp_path / "namespace"
    root.mkdir()
    harness = ProcessRehearsal(_capture_request(root))
    harness._capture_authorization = _load(root / "authorization.json")
    monkeypatch.setattr(process_module, "_replay_namespace_for_job", lambda _job: "wrong")
    with pytest.raises(RuntimeError, match="namespace"):
        harness._validate_capture_namespace(SimpleNamespace())


def test_capture_namespace_uses_the_pipeline_provider_identity_without_double_hash(
    tmp_path: Path,
) -> None:
    job = SimpleNamespace(
        configuration_sha256="a" * 64,
        pipeline_contract_version=SimpleNamespace(value="card_centric_v2"),
        model_config_sha256="b" * 64,
        source_revision_hashes={7: "c" * 64},
        index_snapshot_id="snapshot",
        companion_generation="companion",
        semantic_generation="semantic",
        source_index_generation="source-index",
    )
    direct = replay_namespace_from_job_source(
        configuration_sha256=job.configuration_sha256,
        pipeline_contract_version=job.pipeline_contract_version.value,
        model_config_sha256=job.model_config_sha256,
        source_revision_hashes=job.source_revision_hashes,
        index_snapshot_id=job.index_snapshot_id,
        companion_generation=job.companion_generation,
        semantic_generation=job.semantic_generation,
        source_index_generation=job.source_index_generation,
    )
    assert process_module._replay_namespace_for_job(job) == direct
    assert process_module._replay_namespace_for_job(job) != hashlib.sha256(
        direct.encode()
    ).hexdigest()
    authorization = _load(tmp_path / "authorization.json")
    authorization.document["replay_namespace"] = direct
    harness = ProcessRehearsal(_capture_request(tmp_path))
    harness._capture_authorization = authorization
    harness._validate_capture_namespace(job)


def test_capture_reconciliation_accepts_response_backed_repair_and_contract_topologies(
    tmp_path: Path,
) -> None:
    def reconcile(topologies: list[tuple[str, ...]]) -> None:
        root = tmp_path / str(len(topologies)) / "-".join(topologies[0])
        root.mkdir(parents=True)
        store = CaptureStore(root / "private", _load(root / "authorization.json"))
        store.prepare()
        rows: list[dict[str, object]] = []
        event_id = 1
        for call, topology in enumerate(topologies):
            request = f"{call + 1:064x}"
            replay = _identity() | {
                "subcall_ordinal": call,
                "call_kind": "primary" if call == 0 else "repair",
            }
            parameters = {
                "thinking": "disabled",
                "thinking_budget_tokens": 1024,
                "temperature": None,
                "max_tokens": None,
            }
            replay_request = {
                "kind": "structured",
                "provider": "openrouter",
                "model": "test-model",
                "instruction_sha256": "a" * 64,
                "input_sha256": "b" * 64,
                "output_schema_sha256": "c" * 64,
                "cache_prefix_sha256": None,
                "generation_parameters": parameters,
                "replay_identity": replay,
            }
            private_key = structured_request_key_from_hashes(
                provider="openrouter",
                model="test-model",
                instruction_sha256="a" * 64,
                input_sha256="b" * 64,
                output_schema_sha256="c" * 64,
                cache_prefix_sha256=None,
                generation_parameters=parameters,
                attempt_identity=replay,
            )
            replay_request["key"] = private_key
            ordinal = store.reserve(
                kind="structured",
                provider="openrouter",
                model="test-model",
                request_sha256=request,
                replay_identity=replay,
                replay_request=replay_request,
            )
            response = GeneratedText(
                '{"ok":true}', ProviderName.OPENROUTER, "test-model", "request", 0, 0, 0
            )
            store.record_structured(private_key, response)
            response_sha256 = capture_module._provider_response_sha256(response.text)
            store.bind_private_response(
                ordinal,
                response_sha256,
                replay_request,
            )
            store.complete(ordinal, observed_microusd=0, stored=True)
            for event in topology:
                row = {
                    "id": event_id,
                    "event": event,
                    "provider": "openrouter",
                    "model": "test-model",
                    "request_sha256": request,
                    "stage": "card_prefilter",
                    "kind": "primary" if call == 0 else "repair",
                    "batch_index": 0,
                    "batch_note_ids_sha256": "0" * 64,
                    "subcall_ordinal": call,
                }
                if event == "response_received":
                    row["response_sha256"] = response_sha256
                    row.update(
                        {
                            "request_id": "request",
                            "provider": "openrouter",
                            "model": "test-model",
                            "instruction_sha256": "a" * 64,
                            "input_sha256": "b" * 64,
                            "output_schema_sha256": "c" * 64,
                            "cache_prefix_sha256": None,
                            "generation_parameters": {
                                "thinking": "disabled",
                                "thinking_budget_tokens": 1024,
                                "temperature": None,
                                "max_tokens": 10,
                            },
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "cost_microusd": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        }
                    )
                rows.append(row)
                event_id += 1
        harness = ProcessRehearsal(_capture_request(root))
        harness._capture_store = store
        harness._reconcile_capture_calls(rows)

    reconcile(
        [
            ("begun", "dispatched", "response_received", "validation_failed"),
            ("begun", "dispatched", "response_received", "accepted"),
        ]
    )
    reconcile([("begun", "dispatched", "response_received", "contract_failed")])
    reconcile(
        [("begun", "dispatched", "response_received", "accepted", "contract_failed")]
    )


@pytest.mark.parametrize(
    "topology",
    (
        ("begun", "dispatched", "transport_failed"),
        ("begun", "dispatched", "response_received"),
        ("begun", "response_received", "dispatched", "accepted"),
        ("begun", "dispatched", "response_received", "accepted", "validation_failed"),
    ),
)
def test_capture_reconciliation_rejects_unusable_response_lifecycles(
    tmp_path: Path, topology: tuple[str, ...]
) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    ordinal = store.reserve(
        kind="structured",
        provider="openrouter",
        model="test-model",
        request_sha256="a" * 64,
        replay_identity=_identity() | {"call_kind": "primary"},
    )
    store.complete(ordinal, observed_microusd=0, stored=True)
    rows: list[dict[str, object]] = []
    for event_id, event in enumerate(topology, start=1):
        row = {
            "id": event_id,
            "event": event,
            "provider": "openrouter",
            "model": "test-model",
            "request_sha256": "a" * 64,
            "stage": "card_prefilter",
            "kind": "primary",
            "batch_index": 0,
            "batch_note_ids_sha256": "0" * 64,
            "subcall_ordinal": 0,
        }
        if event == "response_received":
            row["response_sha256"] = "a" * 64
        rows.append(row)
    harness = ProcessRehearsal(_capture_request(tmp_path))
    harness._capture_store = store
    with pytest.raises(RuntimeError, match="topology"):
        harness._reconcile_capture_calls(rows)


def test_capture_app_provider_factory_disables_environment_proxy_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    clients: list[object] = []
    resolved = SimpleNamespace(
        openai_input_usd_per_million=2.5,
        openai_output_usd_per_million=15.0,
        anki_rehearsal_mode="shadow",
    )

    def client(**kwargs: object) -> object:
        value = object()
        clients.append(value)
        calls.append(("http", kwargs))
        return value

    def constructor(name: str):
        def build(**kwargs: object) -> dict[str, object]:
            calls.append((name, kwargs))
            return kwargs

        return build

    monkeypatch.setattr(app_module.httpx, "Client", client)
    monkeypatch.setattr(app_module, "OpenAIProvider", constructor("openai"))
    monkeypatch.setattr(app_module, "GeminiProvider", constructor("gemini"))
    monkeypatch.setattr(app_module, "AnthropicProvider", constructor("anthropic"))
    monkeypatch.setattr(app_module, "OpenRouterProvider", constructor("openrouter"))
    providers, capture_clients = app_module._provider_clients(resolved, object())  # type: ignore[arg-type]
    assert tuple(capture_clients) == tuple(clients) and len(clients) == 4
    assert [kwargs for name, kwargs in calls if name == "http"] == [
        {"timeout": 300.0, "trust_env": False}
    ] * 4
    assert providers[ProviderName.OPENAI]["http"] is clients[0]
    assert providers[ProviderName.OPENAI]["input_usd_per_million"] == 2.5
    assert providers[ProviderName.OPENAI]["output_usd_per_million"] == 15.0
    calls.clear()
    _providers, ordinary_clients = app_module._provider_clients(resolved, None)
    assert ordinary_clients == () and not any(name == "http" for name, _ in calls)


def test_capture_prepared_verification_fails_before_wrapper_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    monkeypatch.setattr(
        store, "_read_ledger", lambda: (_ for _ in ()).throw(CaptureDenied("bad ledger"))
    )
    with pytest.raises(CaptureDenied, match="bad ledger"):
        store.verify_prepared()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode proof")
@pytest.mark.parametrize("mode", (0o755, 0o070, 0o777))
def test_capture_child_rechecks_posix_private_root_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    events: list[str] = []
    resolved = SimpleNamespace(
        anki_rehearsal_capture_store=store.root,
        anki_rehearsal_capture_authorization_manifest=tmp_path / "authorization.json",
        anki_rehearsal_capture_authorization_sha256=store.authorization.sha256,
        anki_rehearsal_capture_candidate_commit=_COMMIT,
        anki_rehearsal_capture_candidate_tree=_TREE,
        anki_rehearsal_capture_capsule_manifest_sha256=_CAPSULE,
        anki_rehearsal_capture_failed_job_id=_JOB,
        anki_rehearsal_mode="shadow",
    )
    monkeypatch.setattr(
        app_module.CaptureAuthorization,
        "load",
        lambda *_args, **_kwargs: store.authorization,
    )
    monkeypatch.setattr(app_module, "CaptureStore", lambda *_args: store)
    monkeypatch.setattr(app_module, "KeyringSecretStore", lambda: events.append("keyring"))
    os.chmod(store.root, mode)
    try:
        with pytest.raises(CaptureDenied, match="private directory permissions"):
            app_module._capture_dependencies(resolved)  # type: ignore[arg-type]
        assert events == []
    finally:
        os.chmod(store.root, 0o700)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink proof")
def test_capture_child_rechecks_private_pack_topology_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    displaced = tmp_path / "displaced-pack"
    store.pack.rename(displaced)
    os.symlink(displaced, store.pack, target_is_directory=True)
    events: list[str] = []
    resolved = SimpleNamespace(
        anki_rehearsal_capture_store=store.root,
        anki_rehearsal_capture_authorization_manifest=tmp_path / "authorization.json",
        anki_rehearsal_capture_authorization_sha256=store.authorization.sha256,
        anki_rehearsal_capture_candidate_commit=_COMMIT,
        anki_rehearsal_capture_candidate_tree=_TREE,
        anki_rehearsal_capture_capsule_manifest_sha256=_CAPSULE,
        anki_rehearsal_capture_failed_job_id=_JOB,
        anki_rehearsal_mode="shadow",
    )
    monkeypatch.setattr(
        app_module.CaptureAuthorization,
        "load",
        lambda *_args, **_kwargs: store.authorization,
    )
    monkeypatch.setattr(app_module, "CaptureStore", lambda *_args: store)
    monkeypatch.setattr(app_module, "KeyringSecretStore", lambda: events.append("keyring"))
    try:
        with pytest.raises(CaptureDenied, match="private directory"):
            app_module._capture_dependencies(resolved)  # type: ignore[arg-type]
        assert events == []
    finally:
        store.pack.unlink()
        displaced.rename(store.pack)


def test_destination_topology_rejects_aliases_and_immutable_containment_before_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    request = _capture_request(root)
    supplement = tmp_path / "supplement"
    supplement.mkdir()
    protected_hash = hashlib.sha256((request.capsule / "capsule.json").read_bytes()).hexdigest()
    symlink_parent = tmp_path / "indirect"
    target = tmp_path / "target"
    target.mkdir()
    os.symlink(target, symlink_parent, target_is_directory=True)
    immutable_target = tmp_path / "immutable-target"
    immutable_target.mkdir()
    immutable_alias = tmp_path / "immutable-alias"
    os.symlink(immutable_target, immutable_alias, target_is_directory=True)
    dangling_leaf = tmp_path / "dangling-evidence.zip"
    os.symlink(tmp_path / "missing-evidence-target", dangling_leaf)
    dangling_parent = tmp_path / "dangling-parent"
    os.symlink(tmp_path / "missing-parent-target", dangling_parent)
    cases = (
        replace(request, overlay=Path("relative-overlay")),
        replace(request, capture_store=request.capsule / "private"),
        replace(request, evidence_zip=supplement / "evidence.zip", replay_supplement=supplement),
        replace(request, evidence_zip=request.overlay / "evidence.zip"),
        replace(request, overlay=symlink_parent / "overlay"),
        replace(request, evidence_zip=dangling_leaf),
        replace(request, overlay=dangling_parent / "overlay"),
        replace(
            request,
            implementation_repository=immutable_alias,
            overlay=immutable_target / "overlay",
        ),
    )
    for candidate in cases:
        harness = ProcessRehearsal(candidate)
        called: list[str] = []
        with monkeypatch.context() as local:
            local.setattr(
                ProcessRehearsal,
                "_verify_implementation_identity",
                lambda _self: None,
            )
            local.setattr(process_module, "verify_capsule", lambda _path: SimpleNamespace())
            local.setattr(
                harness,
                "_prepare_capture_store",
                lambda calls=called: calls.append("prepare"),
            )
            local.setattr(
                process_module,
                "materialize_capsule",
                lambda *_args, calls=called: calls.append("materialize"),
            )
            with pytest.raises(ValueError):
                harness.run()
        assert called == []
        assert (
            hashlib.sha256((request.capsule / "capsule.json").read_bytes()).hexdigest()
            == protected_hash
        )


def test_capture_environment_exports_absolute_authorization_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    request = _capture_request(root)
    assert request.capture_authorization_manifest is not None
    expected_manifest = request.capture_authorization_manifest.resolve(strict=True)
    monkeypatch.chdir(root)
    harness = ProcessRehearsal(
        replace(request, capture_authorization_manifest=Path("authorization.json"))
    )
    harness._capture_authorization = SimpleNamespace()  # type: ignore[assignment]
    harness._source_tree_sha256 = "c" * 64
    overlay_root = root / "materialized"
    prompt_directory = overlay_root / "sources/repository/src/oms_hub/anki/prompt_assets"
    prompt_directory.mkdir(parents=True)
    (overlay_root / "sources/a0data").mkdir(parents=True)
    overlay = SimpleNamespace(root=overlay_root, database_path=overlay_root / "hub/hub.db")
    manifest = SimpleNamespace(
        logical_roots={"a0data": "sources/a0data", "repository": "sources/repository"}
    )
    environment = harness._environment(overlay, manifest)
    exported = Path(environment["OMS_HUB_ANKI_REHEARSAL_CAPTURE_AUTHORIZATION_MANIFEST"])
    assert exported.is_absolute()
    assert exported == expected_manifest


def test_capture_dependency_factory_aborts_before_keyring_or_provider_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    authorization = SimpleNamespace(document={"structured": [{"provider": "openrouter"}]})
    resolved = SimpleNamespace(
        anki_rehearsal_capture_store=Path("/private/capture"),
        anki_rehearsal_capture_authorization_manifest=Path("/private/auth.json"),
        anki_rehearsal_capture_authorization_sha256="0" * 64,
        anki_rehearsal_capture_candidate_commit=_COMMIT,
        anki_rehearsal_capture_candidate_tree=_TREE,
        anki_rehearsal_capture_capsule_manifest_sha256=_CAPSULE,
        anki_rehearsal_capture_failed_job_id=_JOB,
        anki_rehearsal_mode="shadow",
    )

    class Store:
        def __init__(self, *_args: object) -> None:
            events.append("store")

        def verify_prepared(self) -> None:
            events.append("verify")
            raise CaptureDenied("prepared failed")

    monkeypatch.setattr(
        app_module.CaptureAuthorization, "load", lambda *_args, **_kwargs: authorization
    )
    monkeypatch.setattr(app_module, "CaptureStore", Store)
    monkeypatch.setattr(app_module, "KeyringSecretStore", lambda: events.append("keyring"))
    with pytest.raises(CaptureDenied, match="prepared failed"):
        app_module._capture_dependencies(resolved)  # type: ignore[arg-type]
    assert events == ["store", "verify"]


def test_capture_helpers_reject_nonshadow_before_credentials_or_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    resolved = SimpleNamespace(
        anki_rehearsal_capture_store=Path("/private/capture"),
        anki_rehearsal_mode="off",
        openai_input_usd_per_million=2.5,
        openai_output_usd_per_million=15.0,
    )
    monkeypatch.setattr(
        app_module.CaptureAuthorization,
        "load",
        lambda *_args, **_kwargs: events.append("authorization"),
    )
    monkeypatch.setattr(app_module, "KeyringSecretStore", lambda: events.append("keyring"))
    monkeypatch.setattr(app_module.httpx, "Client", lambda **_kwargs: events.append("http"))
    for name in ("OpenAIProvider", "GeminiProvider", "AnthropicProvider", "OpenRouterProvider"):
        monkeypatch.setattr(
            app_module, name, lambda **_kwargs: events.append("provider")
        )
    with pytest.raises(ValueError, match="shadow"):
        app_module._capture_dependencies(resolved)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="shadow"):
        app_module._provider_clients(resolved, object())  # type: ignore[arg-type]
    assert events == []


def test_capture_stage_attempt_limit_is_one_and_ordinary_modes_keep_configuration() -> None:
    resolved = SimpleNamespace(anki_worker_max_stage_attempts=5)
    assert app_module._stage_attempt_limit(resolved, object()) == 1  # type: ignore[arg-type]
    assert app_module._stage_attempt_limit(resolved, None) == 5  # type: ignore[arg-type]


def test_capture_stage_attempt_limit_stops_retryable_failure_before_second_dispatch() -> None:
    calls: list[str] = []
    job_id = UUID(int=44)

    class Repository:
        def require_job(self, _job_id: UUID) -> SimpleNamespace:
            return SimpleNamespace(
                state=CurationState.CARD_PREFILTERING,
                pipeline_contract_version=PipelineContractVersion.CARD_CENTRIC_V2,
            )

        def get_stage(self, _job_id: UUID, _stage: CurationStage) -> SimpleNamespace:
            return SimpleNamespace(attempt_count=1)

        def defer_job(self, *_args: object, **_kwargs: object) -> None:
            calls.append("defer-second-dispatch")

        def fail_job(self, *_args: object, **_kwargs: object) -> None:
            calls.append("fail-without-retry")

    resolved = SimpleNamespace(anki_worker_max_stage_attempts=5)
    worker = AnkiCurationWorker(
        Repository(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        worker_id="capture",
        max_stage_attempts=app_module._stage_attempt_limit(resolved, object()),  # type: ignore[arg-type]
    )
    asyncio.run(
        worker._handle_failure(  # noqa: SLF001 - direct retry boundary
            job_id, CurationState.CARD_PREFILTERING, VoyageEmbeddingError("retryable")
        )
    )
    assert calls == ["fail-without-retry"]


def test_capture_selects_hash_only_repository_without_changing_other_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = object()
    monkeypatch.setattr(
        app_module, "CaptureAnkiCurationRepository", lambda value: ("capture", value)
    )
    monkeypatch.setattr(app_module, "AnkiCurationRepository", lambda value: ("ordinary", value))
    assert app_module._anki_curation_repository(database, object()) == ("capture", database)  # type: ignore[arg-type]
    assert app_module._anki_curation_repository(database, None) == ("ordinary", database)  # type: ignore[arg-type]


def test_capture_repository_persists_hash_only_response_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProviderAttemptIdentity(
        UUID(int=9), CurationStage.CARD_PREFILTER, 1, "shadow", 1, 0, (), "primary"
    )
    event = ProviderAttemptEvent(
        identity, "response_received", "a" * 64, "request", "b" * 64
    )
    original = ProviderEventEvidence(
        event=event,
        provider="openrouter",
        model="test-model",
        instruction_sha256="c" * 64,
        input_sha256="d" * 64,
        output_schema_sha256="e" * 64,
        generation_parameters={"max_tokens": 10},
        generation_parameters_sha256="f" * 64,
        cache_prefix_sha256=None,
        request_id="request",
        input_tokens=2,
        output_tokens=1,
        cost_microusd=7,
        response_text="private-provider-response-sentinel",
    )
    received: list[ProviderEventEvidence] = []
    monkeypatch.setattr(
        AnkiCurationRepository,
        "record_provider_attempt_event",
        lambda _self, evidence, **_kwargs: received.append(evidence),
    )
    CaptureAnkiCurationRepository(object()).record_provider_attempt_event(  # type: ignore[arg-type]
        original, lease_owner="lease"
    )
    assert received == [replace(original, response_text=None)]
    assert received[0].event is original.event
    assert received[0].event.response_sha256 == original.event.response_sha256
    assert received[0].input_tokens == original.input_tokens
    assert received[0].output_tokens == original.output_tokens
    assert received[0].cost_microusd == original.cost_microusd
    assert original.response_text == "private-provider-response-sentinel"


def test_structured_capture_budget_boundaries_precede_inner_call(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    calls: list[object] = []

    class Inner:
        def generate_text(self, *_args: object, **_kwargs: object) -> GeneratedText:
            calls.append(object())
            return GeneratedText("{}", ProviderName.OPENROUTER, "test-model", "id", 0, 0, 0)

    wrapper = CaptureStructuredTextGenerator(
        Inner(), store, {ProviderName.OPENROUTER: "https://openrouter.example/v1/chat"}
    )
    binding = ProviderAttemptBinding(
        UUID(int=1),
        CurationStage.CARD_FAST_CLASSIFY,
        1,
        "shadow",
        lambda _event: None,
        replay_namespace="capture-test",
    )
    with bind_provider_attempts(binding), provider_call_scope(batch_index=0):
        begin_provider_call(
            provider="openrouter",
            model="test-model",
            instruction="i",
            input_text="x",
            output_schema={},
            generation_parameters={
                "thinking": "disabled",
                "thinking_budget_tokens": 1024,
                "temperature": None,
                "max_tokens": None,
            },
            cacheable_source_prefix=None,
        )
        with pytest.raises(CaptureDenied, match="output-token"):
            wrapper.generate_text(
                "i", "x", output_schema={}, provider=ProviderName.OPENROUTER, model="test-model"
            )
    assert not calls
    assert (
        store.authorization.structured_route(
            ProviderName.OPENROUTER, "test-model", "https://openrouter.example/v1/chat"
        )["input_microusd_per_million"]
        == 1
    )


def test_capture_job_plan_rejects_missing_extra_and_mismatched_routes(tmp_path: Path) -> None:
    harness = ProcessRehearsal(_capture_request(tmp_path))
    harness._capture_authorization = _load(tmp_path / "authorization.json")
    allowed = SimpleNamespace(provider="openrouter", model="test-model")
    wrong = SimpleNamespace(provider="openai", model="other")
    job = SimpleNamespace(
        resolved_model_config=SimpleNamespace(
            ledger_s2=allowed,
            classify_s4=allowed,
            residual_s6=allowed,
            gap_fill_s7=wrong,
            fast_classify_s4b=None,
        )
    )
    with pytest.raises(RuntimeError, match="routes"):
        harness._validate_capture_job_routes(job)


def test_populated_pack_completion_bindings_require_exact_hash(tmp_path: Path) -> None:
    supplement = tmp_path / "supplement"
    manifest_sha256 = _write_replay_supplement(supplement, populated=True)
    completion = tmp_path / "capture-completion.json"
    document = {
        "schema_version": 1,
        "authorization_sha256": "a" * 64,
        "candidate": {"commit": _COMMIT, "tree": _TREE},
        "capsule_manifest_sha256": _CAPSULE,
        "failed_job_id": _JOB,
        "job_id": str(UUID(int=1)),
        "source_tree_sha256": "b" * 64,
        "replay_namespace_sha256": "c" * 64,
        "server_audit_sha256": "d" * 64,
        "pack_manifest_sha256": "d" * 64,
        "ledger_sha256": "e" * 64,
    }
    completion.write_text(json.dumps(document), encoding="utf-8")
    digest = hashlib.sha256(completion.read_bytes()).hexdigest()
    _verify_replay_completion(
        completion,
        digest,
        supplement_root=supplement,
        expected_manifest_sha256=manifest_sha256,
        expected_commit=_COMMIT,
        expected_tree=_TREE,
        expected_capsule_sha256=_CAPSULE,
        expected_pack_sha256="d" * 64,
    )
    with pytest.raises(ValueError, match="unavailable"):
        _verify_replay_completion(
            completion,
            "0" * 64,
            supplement_root=supplement,
            expected_manifest_sha256=manifest_sha256,
            expected_commit=_COMMIT,
            expected_tree=_TREE,
            expected_capsule_sha256=_CAPSULE,
            expected_pack_sha256="d" * 64,
        )
    with pytest.raises(ValueError, match="bindings"):
        _verify_replay_completion(
            completion,
            digest,
            supplement_root=supplement,
            expected_manifest_sha256=manifest_sha256,
            expected_commit="f" * 40,
            expected_tree=_TREE,
            expected_capsule_sha256=_CAPSULE,
            expected_pack_sha256="d" * 64,
        )
    with pytest.raises(ValueError, match="failed-job lineage"):
        _verify_replay_completion(
            completion,
            digest,
            supplement_root=supplement,
            expected_manifest_sha256=manifest_sha256,
            expected_commit=_COMMIT,
            expected_tree=_TREE,
            expected_capsule_sha256=_CAPSULE,
            expected_pack_sha256="d" * 64,
            expected_failed_job_id=str(UUID(int=99)),
            expected_replay_namespace="c" * 64,
        )
    with pytest.raises(ValueError, match="namespace lineage"):
        _verify_replay_completion(
            completion,
            digest,
            supplement_root=supplement,
            expected_manifest_sha256=manifest_sha256,
            expected_commit=_COMMIT,
            expected_tree=_TREE,
            expected_capsule_sha256=_CAPSULE,
            expected_pack_sha256="d" * 64,
            expected_failed_job_id=_JOB,
            expected_replay_namespace="f" * 64,
        )
    _verify_replay_completion(
        completion,
        digest,
        supplement_root=supplement,
        expected_manifest_sha256=manifest_sha256,
        expected_commit=_COMMIT,
        expected_tree=_TREE,
        expected_capsule_sha256=_CAPSULE,
        expected_pack_sha256="d" * 64,
        expected_failed_job_id=_JOB,
        expected_replay_namespace="c" * 64,
    )


def test_populated_completion_rejects_server_audit_digest_swap(tmp_path: Path) -> None:
    supplement = tmp_path / "supplement"
    manifest_sha256 = _write_replay_supplement(supplement, populated=True)
    completion = tmp_path / "completion.json"
    digest = _write_completion(
        completion,
        capsule=_CAPSULE,
        pack_manifest="d" * 64,
        server_audit_sha256="e" * 64,
    )
    with pytest.raises(ValueError, match="lineage"):
        _verify_replay_completion(
            completion,
            digest,
            supplement_root=supplement,
            expected_manifest_sha256=manifest_sha256,
            expected_commit=_COMMIT,
            expected_tree=_TREE,
            expected_capsule_sha256=_CAPSULE,
            expected_pack_sha256="d" * 64,
        )


def test_structured_utf8_budget_and_rounding_reservation_boundaries(tmp_path: Path) -> None:
    authorization = _load(tmp_path / "authorization.json")
    route = authorization.document["structured"][0]
    route["max_input_bytes"] = 1
    store = CaptureStore(tmp_path / "private", authorization)
    store.prepare()
    invoked = False

    class Inner:
        def generate_text(self, *_args: object, **_kwargs: object) -> GeneratedText:
            nonlocal invoked
            invoked = True
            return GeneratedText("{}", ProviderName.OPENROUTER, "test-model", "id", 0, 0, 0)

    wrapper = CaptureStructuredTextGenerator(
        Inner(), store, {ProviderName.OPENROUTER: "https://openrouter.example/v1/chat"}
    )
    binding = ProviderAttemptBinding(
        UUID(int=8),
        CurationStage.CARD_FAST_CLASSIFY,
        1,
        "shadow",
        lambda _event: None,
        replay_namespace="capture-test",
    )
    with bind_provider_attempts(binding), provider_call_scope(batch_index=0):
        begin_provider_call(
            provider="openrouter",
            model="test-model",
            instruction="é",
            input_text="x",
            output_schema={},
            generation_parameters={
                "thinking": "disabled",
                "thinking_budget_tokens": 1024,
                "temperature": None,
                "max_tokens": 1,
            },
            cacheable_source_prefix=None,
        )
        with pytest.raises(CaptureDenied, match="input-byte"):
            wrapper.generate_text(
                "é",
                "x",
                output_schema={},
                provider=ProviderName.OPENROUTER,
                model="test-model",
                options=GenerationOptions(max_tokens=1),
            )
    assert invoked is False
    route["max_input_bytes"] = 10_000
    route["input_microusd_per_million"] = 1_000_000
    route["output_microusd_per_million"] = 1_000_000
    route["max_reserved_microusd"] = 10_000
    with bind_provider_attempts(binding), provider_call_scope(batch_index=1):
        begin_provider_call(
            provider="openrouter",
            model="test-model",
            instruction="a",
            input_text="b",
            output_schema={},
            generation_parameters={
                "thinking": "disabled",
                "thinking_budget_tokens": 1024,
                "temperature": None,
                "max_tokens": 1,
            },
            cacheable_source_prefix=None,
        )
        wrapper.generate_text(
            "a",
            "b",
            output_schema={},
            provider=ProviderName.OPENROUTER,
            model="test-model",
            options=GenerationOptions(max_tokens=1),
        )
    reserved = store.calls()[-1]["reserved_microusd"]
    assert isinstance(reserved, int) and reserved >= 2


def _write_replay_supplement(root: Path, *, populated: bool) -> str:
    root.mkdir()
    structured = {"stable-key": {"text": "{}"}} if populated else {}
    structured_path = root / "structured.json"
    structured_path.write_text(json.dumps(structured, sort_keys=True), encoding="utf-8")
    vector_root = root / "vectors"
    vector_root.mkdir()
    (vector_root / "manifest.json").write_text("{}", encoding="utf-8")
    lineage_path = root / "capture-lineage.json"
    if populated:
        lineage_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "authorization_sha256": "a" * 64,
                    "failed_job_id": _JOB,
                    "source_tree_sha256": "b" * 64,
                    "replay_namespace_sha256": "c" * 64,
                    "server_audit_sha256": "d" * 64,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    files = []
    paths = [structured_path, vector_root / "manifest.json"]
    if populated:
        paths.append(lineage_path)
    for path in paths:
        raw = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    manifest = {"schema_version": 1, "manifest_rule": "self-excluding", "files": files}
    path = root / "replay-supplement.json"
    path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_completion(
    path: Path,
    *,
    capsule: str,
    pack_manifest: str,
    candidate: str = _COMMIT,
    authorization_sha256: str = "a" * 64,
    failed_job_id: str = _JOB,
    source_tree_sha256: str = "b" * 64,
    replay_namespace_sha256: str = "c" * 64,
    server_audit_sha256: str = "d" * 64,
) -> str:
    document = {
        "schema_version": 1,
        "authorization_sha256": authorization_sha256,
        "candidate": {"commit": candidate, "tree": _TREE},
        "capsule_manifest_sha256": capsule,
        "failed_job_id": failed_job_id,
        "job_id": str(UUID(int=5)),
        "source_tree_sha256": source_tree_sha256,
        "replay_namespace_sha256": replay_namespace_sha256,
        "server_audit_sha256": server_audit_sha256,
        "pack_manifest_sha256": pack_manifest,
        "ledger_sha256": "d" * 64,
    }
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_populated_golden_requires_bound_detached_completion_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ProcessRehearsal, "_verify_implementation_identity", lambda self: None)
    monkeypatch.setattr(process_module, "verify_capsule", lambda _path: SimpleNamespace())
    supplement = tmp_path / "supplement"
    pack_sha256 = _write_replay_supplement(supplement, populated=True)
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    seed = _capture_request(seed_root)
    completion = tmp_path / "completion.json"
    completion_sha256 = _write_completion(
        completion,
        capsule=seed.expected_manifest_sha256,
        pack_manifest=pack_sha256,
    )
    request = replace(
        seed,
        mode="deterministic",
        shadow_egress_pins_json=None,
        run_goal="golden",
        replay_supplement=supplement,
        expected_replay_supplement_manifest_sha256=pack_sha256,
        replay_supplement_completion=completion,
        expected_replay_supplement_completion_sha256=completion_sha256,
    )
    assert ProcessRehearsal(request)._validate_destinations() is not None
    for change in (
        {"replay_supplement_completion": None},
        {"expected_replay_supplement_completion_sha256": "0" * 64},
        {
            "replay_supplement_completion": tmp_path / "wrong-candidate.json",
            "expected_replay_supplement_completion_sha256": _write_completion(
                tmp_path / "wrong-candidate.json",
                capsule=seed.expected_manifest_sha256,
                pack_manifest=pack_sha256,
                candidate="f" * 40,
            ),
        },
        {
            "replay_supplement_completion": tmp_path / "wrong-capsule.json",
            "expected_replay_supplement_completion_sha256": _write_completion(
                tmp_path / "wrong-capsule.json", capsule="e" * 64, pack_manifest=pack_sha256
            ),
        },
        {
            "replay_supplement_completion": tmp_path / "wrong-pack.json",
            "expected_replay_supplement_completion_sha256": _write_completion(
                tmp_path / "wrong-pack.json",
                capsule=seed.expected_manifest_sha256,
                pack_manifest="e" * 64,
            ),
        },
    ):
        with pytest.raises(ValueError, match="completion manifest"):
            ProcessRehearsal(replace(request, **change))._validate_destinations()
    indirect = tmp_path / "indirect-completion.json"
    indirect.symlink_to(completion)
    with pytest.raises(ValueError, match="completion manifest"):
        ProcessRehearsal(
            replace(
                request,
                replay_supplement_completion=indirect,
                expected_replay_supplement_completion_sha256=completion_sha256,
            )
        )._validate_destinations()


def test_populated_completion_must_match_manifest_covered_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ProcessRehearsal, "_verify_implementation_identity", lambda self: None)
    monkeypatch.setattr(process_module, "verify_capsule", lambda _path: SimpleNamespace())
    supplement = tmp_path / "supplement"
    pack_sha256 = _write_replay_supplement(supplement, populated=True)
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    seed = _capture_request(seed_root)
    for index, changes in enumerate(
        (
            {"authorization_sha256": "f" * 64},
            {"failed_job_id": str(UUID(int=9))},
            {"source_tree_sha256": "f" * 64},
            {"replay_namespace_sha256": "f" * 64},
        )
    ):
        completion = tmp_path / f"completion-{index}.json"
        digest = _write_completion(
            completion,
            capsule=seed.expected_manifest_sha256,
            pack_manifest=pack_sha256,
            **changes,
        )
        request = replace(
            seed,
            mode="deterministic",
            shadow_egress_pins_json=None,
            run_goal="golden",
            replay_supplement=supplement,
            expected_replay_supplement_manifest_sha256=pack_sha256,
            replay_supplement_completion=completion,
            expected_replay_supplement_completion_sha256=digest,
        )
        with pytest.raises(ValueError, match="lineage"):
            ProcessRehearsal(request)._validate_destinations()


def test_first_replay_miss_forbids_completion_but_accepts_empty_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ProcessRehearsal, "_verify_implementation_identity", lambda self: None)
    monkeypatch.setattr(process_module, "verify_capsule", lambda _path: SimpleNamespace())
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    seed = _capture_request(seed_root)
    supplement = tmp_path / "empty"
    pack_sha256 = _write_replay_supplement(supplement, populated=False)
    request = replace(
        seed,
        mode="deterministic",
        shadow_egress_pins_json=None,
        run_goal="first_replay_miss",
        restart_after_durable_boundary=False,
        replay_supplement=supplement,
        expected_replay_supplement_manifest_sha256=pack_sha256,
        replay_supplement_completion=None,
        expected_replay_supplement_completion_sha256=None,
    )
    assert ProcessRehearsal(request)._validate_destinations() is not None
    with pytest.raises(ValueError, match="forbids"):
        ProcessRehearsal(
            replace(request, replay_supplement_completion=tmp_path / "completion.json")
        )._validate_destinations()


def test_capture_completion_publication_faults_never_create_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def prepared(root: Path) -> tuple[ProcessRehearsal, CaptureStore]:
        request = _capture_request(root)
        store = CaptureStore(root / "private", _load(root / "authorization.json"))
        store.prepare()
        store.initialize_server_audit("a" * 32)
        ordinal = store.reserve(kind="structured", output_tokens=1, replay_identity=_identity())
        store.record_structured(
            "stable-key",
            GeneratedText("{}", ProviderName.OPENROUTER, "test-model", "request", 0, 0, 0),
        )
        store.complete(ordinal, observed_microusd=0, stored=True)
        store.record_vectors(
            ["query"],
            np.asarray([[1.0, 0.0]], dtype=np.float32),
            model="voyage-4-large",
            dimensions=2,
            input_type="query",
        )
        harness = ProcessRehearsal(request)
        harness._capture_store = store
        harness._capture_authorization = store.authorization
        harness._source_tree_sha256 = "e" * 64
        return harness, store

    for boundary in ("zip-write", "zip-verify", "pack", "completion"):
        root = tmp_path / boundary
        root.mkdir()
        harness, store = prepared(root)
        monkeypatch.setattr(harness, "_validate_capture_namespace", lambda _job: None)
        monkeypatch.setattr(harness, "_reconcile_capture_calls", lambda _rows: None)
        monkeypatch.setattr(process_module, "_replay_namespace_for_job", lambda _job: "f" * 64)
        if boundary == "zip-write":
            monkeypatch.setattr(
                    process_module,
                    "_write_deterministic_zip",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("zip")),
            )
        elif boundary == "zip-verify":
            monkeypatch.setattr(
                process_module,
                "_verify_evidence_zip",
                lambda *_args: (_ for _ in ()).throw(RuntimeError("verify")),
            )
        elif boundary == "pack":
            monkeypatch.setattr(
                store,
                "publish_pack_manifest",
                lambda *_args: (_ for _ in ()).throw(CaptureDenied("pack")),
            )
        else:
            monkeypatch.setattr(
                store,
                "write_completion",
                lambda *_args: (_ for _ in ()).throw(CaptureDenied("completion")),
            )
        with pytest.raises((OSError, RuntimeError, CaptureDenied)):
            harness._write_capture_completion(
                SimpleNamespace(model_dump=lambda **_kwargs: {}),
                SimpleNamespace(),
                SimpleNamespace(transcript=[]),
                SimpleNamespace(
                    require_job=lambda _job: SimpleNamespace(),
                    list_provider_attempt_events=lambda _job: [],
                ),
                UUID(int=3),
                {"records": []},
                {"records": []},
                store.server_audit(),
            )
        assert not (store.root / "capture-completion.json").exists()
        if boundary != "completion":
            assert not (store.pack / "replay-supplement.json").exists()
        monkeypatch.undo()


def test_capture_evidence_excludes_raw_provider_response_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "capture"
    root.mkdir()
    request = _capture_request(root)
    authorization = _load(root / "authorization.json")
    store = CaptureStore(root / "private", authorization)
    store.prepare()
    store.initialize_server_audit("a" * 32)
    sentinel = "capture-private-response-sentinel-9f8c"
    ordinal = store.reserve(
        kind="structured", output_tokens=1, cost_microusd=7, replay_identity=_identity()
    )
    store.record_structured(
        "stable-key",
        GeneratedText(sentinel, ProviderName.OPENROUTER, "test-model", "request", 1, 1, 7),
    )
    store.complete(ordinal, observed_microusd=7, stored=True)
    store.record_vectors(
        ["query"],
        np.asarray([[1.0, 0.0]], dtype=np.float32),
        model="voyage-4-large",
        dimensions=2,
        input_type="query",
    )
    row = {
        "event": "accepted",
        "provider": "openrouter",
        "model": "test-model",
        "request_sha256": "a" * 64,
        "response_sha256": hashlib.sha256(sentinel.encode()).hexdigest(),
        "response_text": sentinel,
        "stage": "card_prefilter",
        "kind": "primary",
        "batch_index": 0,
        "batch_note_ids_sha256": "0" * 64,
        "subcall_ordinal": 0,
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_microusd": 7,
        "request_id": "request",
        "validation_error": None,
        "diagnostic_source": None,
        "http_status": None,
    }
    harness = ProcessRehearsal(request)
    harness._capture_store = store
    harness._capture_authorization = authorization
    harness._source_tree_sha256 = "e" * 64
    reconciled: list[list[dict[str, object]]] = []
    monkeypatch.setattr(harness, "_validate_capture_namespace", lambda _job: None)
    monkeypatch.setattr(harness, "_reconcile_capture_calls", lambda rows: reconciled.append(rows))
    monkeypatch.setattr(process_module, "_replay_namespace_for_job", lambda _job: "f" * 64)
    repository = SimpleNamespace(
        require_job=lambda _job: SimpleNamespace(), list_provider_attempt_events=lambda _job: [row]
    )
    harness._write_capture_completion(
        SimpleNamespace(model_dump=lambda **_kwargs: {}),
        SimpleNamespace(),
        SimpleNamespace(transcript=[]),
        repository,
        UUID(int=3),
        {"records": []},
        {"records": []},
        store.server_audit(),
    )
    assert reconciled == [[row]]
    assert sentinel in (store.pack / "structured.json").read_text(encoding="utf-8")
    overlay_evidence: list[ProviderEventEvidence] = []
    identity = ProviderAttemptIdentity(
        UUID(int=3), CurationStage.CARD_PREFILTER, 1, "shadow", 1, 0, (), "primary"
    )
    monkeypatch.setattr(
        AnkiCurationRepository,
        "record_provider_attempt_event",
        lambda _self, evidence, **_kwargs: overlay_evidence.append(evidence),
    )
    CaptureAnkiCurationRepository(object()).record_provider_attempt_event(  # type: ignore[arg-type]
        ProviderEventEvidence(
            event=ProviderAttemptEvent(
                identity, "response_received", "a" * 64, "request", row["response_sha256"]
            ),
            provider="openrouter",
            model="test-model",
            instruction_sha256="b" * 64,
            input_sha256="c" * 64,
            output_schema_sha256="d" * 64,
            generation_parameters={},
            generation_parameters_sha256="e" * 64,
            cache_prefix_sha256=None,
            request_id="request",
            input_tokens=1,
            output_tokens=1,
            cost_microusd=7,
            response_text=sentinel,
        ),
        lease_owner="lease",
    )
    assert overlay_evidence[0].response_text is None
    assert overlay_evidence[0].event.response_sha256 == row["response_sha256"]
    with zipfile.ZipFile(request.evidence_zip) as archive:
        for name in archive.namelist():
            assert sentinel.encode() not in archive.read(name)
        projected = json.loads(archive.read("provider-attempt-ledger.json"))
        published_audit = archive.read("capture-server-audit.json")
    completion = json.loads((store.root / "capture-completion.json").read_text(encoding="utf-8"))
    lineage = json.loads((store.pack / "capture-lineage.json").read_text(encoding="utf-8"))
    published_audit_sha256 = hashlib.sha256(published_audit).hexdigest()
    assert published_audit == capture_module.serialize_evidence_record(
        store.server_audit_evidence_projection()
    )
    assert capture_module.evidence_redact(store.server_audit_evidence_projection()) == (
        store.server_audit_evidence_projection()
    )
    assert published_audit_sha256 == completion["server_audit_sha256"]
    assert published_audit_sha256 == lineage["server_audit_sha256"]
    assert hashlib.sha256(published_audit.rstrip(b"\n")).hexdigest() != published_audit_sha256
    assert "response_text" not in projected[0]
    for key in (
        "response_sha256",
        "request_sha256",
        "event",
        "provider",
        "model",
        "stage",
        "kind",
            "batch_index",
            "batch_note_ids_sha256",
            "subcall_ordinal",
            "cost_microusd",
        ):
        assert projected[0][key] == row[key]
    assert projected[0]["input_tokens"] == "[REDACTED]"
    assert projected[0]["output_tokens"] == "[REDACTED]"


def test_capture_job_route_closure_uses_exact_code_endpoint_tuples(tmp_path: Path) -> None:
    harness = ProcessRehearsal(_capture_request(tmp_path))
    authorization = _load(tmp_path / "authorization.json")
    model = "gemini 2/flash"
    endpoint = process_module._provider_endpoint("gemini", model)
    assert endpoint.endswith("/gemini%202%2Fflash:generateContent")
    authorization.document["structured"] = [
        {"provider": "gemini", "model": model, "endpoint": endpoint}
    ]
    harness._capture_authorization = authorization
    stage = SimpleNamespace(provider="gemini", model=model)
    job = SimpleNamespace(
        resolved_model_config=SimpleNamespace(
            ledger_s2=stage,
            classify_s4=stage,
            residual_s6=stage,
            gap_fill_s7=stage,
            fast_classify_s4b=None,
        )
    )
    harness._validate_capture_job_routes(job)
    authorization.document["structured"].append(
        {"provider": "gemini", "model": model, "endpoint": endpoint + "?unused=1"}
    )
    with pytest.raises(RuntimeError, match="routes"):
        harness._validate_capture_job_routes(job)


@pytest.mark.parametrize(
    ("stage", "kind"),
    (
        (CurationStage.CARD_LEDGER, "primary"),
        (CurationStage.CARD_LEDGER, "repair"),
        (CurationStage.CARD_FAST_CLASSIFY, "primary"),
        (CurationStage.CARD_CLASSIFY, "primary"),
        (CurationStage.CARD_CLASSIFY, "repair"),
        (CurationStage.CARD_RESIDUAL, "primary"),
        (CurationStage.CARD_RESIDUAL, "repair"),
        (CurationStage.CARD_GAP_FILL, "primary"),
    ),
)
def test_capture_structured_service_injects_bound_before_attempt_and_replays(
    tmp_path: Path, stage: CurationStage, kind: str
) -> None:
    authorization = _load(tmp_path / "authorization.json")
    store = CaptureStore(tmp_path / "private", authorization)
    store.prepare()
    inner_options: list[GenerationOptions] = []
    events: list[object] = []

    class Answer(BaseModel):
        answer: str

    class Inner:
        def generate_text(self, *_args: object, **kwargs: object) -> GeneratedText:
            options = kwargs.get("options")
            assert isinstance(options, GenerationOptions)
            inner_options.append(options)
            return GeneratedText(
                '{"answer":"ok"}', ProviderName.OPENROUTER, "test-model", "request", 1, 1, 0
            )

    service = CaptureStructuredTextService(
        CaptureStructuredTextGenerator(
            Inner(), store, {ProviderName.OPENROUTER: "https://openrouter.example/v1/chat"}
        ),
        authorization,
        {ProviderName.OPENROUTER: "https://openrouter.example/v1/chat"},
    )
    capture_binding = ProviderAttemptBinding(
        UUID(int=7), stage, 1, "shadow", events.append, replay_namespace="capture-test"
    )
    with bind_provider_attempts(capture_binding), provider_call_scope(batch_index=0, kind=kind):
        result = service.generate_json(
            "instruction",
            "input",
            output_model=Answer,
            provider=ProviderName.OPENROUTER,
            model="test-model",
        )
    assert result.value == Answer(answer="ok")
    assert inner_options == [GenerationOptions(max_tokens=10)]
    assert result.attempt_handle is not None
    assert result.attempt_handle.generation_parameters["max_tokens"] == 10
    replay = ReplayStructuredTextGenerator(
        store.pack / "structured.json", require_attempt_identity=True
    )
    replay_service = app_module.StructuredTextService(replay)
    replay_binding = ProviderAttemptBinding(
        UUID(int=8), stage, 1, "canonical", lambda _event: None, replay_namespace="capture-test"
    )
    with bind_provider_attempts(replay_binding), provider_call_scope(batch_index=0, kind=kind):
        replay_result = replay_service.generate_json(
            "instruction",
            "input",
            output_model=Answer,
            provider=ProviderName.OPENROUTER,
            model="test-model",
        )
    assert replay_result.value == Answer(answer="ok")
    events.clear()
    with bind_provider_attempts(capture_binding), provider_call_scope(batch_index=1, kind=kind):
        with pytest.raises(CaptureDenied, match="output-token"):
            service.generate_json(
                "instruction",
                "input",
                output_model=Answer,
                provider=ProviderName.OPENROUTER,
                model="test-model",
                options=GenerationOptions(max_tokens=11),
            )
    assert events == []
    assert len(inner_options) == 1

    class Broken:
        def generate_text(self, *_args: object, **_kwargs: object) -> GeneratedText:
            raise RuntimeError("capture inner failure")

    failing_service = CaptureStructuredTextService(
        CaptureStructuredTextGenerator(
            Broken(), store, {ProviderName.OPENROUTER: "https://openrouter.example/v1/chat"}
        ),
        authorization,
        {ProviderName.OPENROUTER: "https://openrouter.example/v1/chat"},
    )
    with bind_provider_attempts(capture_binding), provider_call_scope(batch_index=2, kind=kind):
        with pytest.raises(RuntimeError, match="inner failure"):
            failing_service.generate_json(
                "instruction",
                "input",
                output_model=Answer,
                provider=ProviderName.OPENROUTER,
                model="test-model",
            )
    assert capture_module._capture_replay_generation_options.get() is None


def test_capture_normalizes_authorized_openai_snapshot_model_for_replay(tmp_path: Path) -> None:
    authorization_path, _digest = _authorization(tmp_path / "authorization.json")
    document = json.loads(authorization_path.read_text(encoding="utf-8"))
    document["structured"] = [
        {
            **document["structured"][0],
            "provider": "openai",
            "model": "gpt-4o-mini",
            "endpoint": "https://api.openai.com/v1/responses",
        }
    ]
    document["egress_pins"] = {
        "api.openai.com": ["203.0.113.1"],
        "api.voyageai.com": ["203.0.113.2"],
    }
    authorization_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    authorization = CaptureAuthorization.load(
        authorization_path,
        hashlib.sha256(authorization_path.read_bytes()).hexdigest(),
        commit=_COMMIT,
        tree=_TREE,
        capsule_sha256=_CAPSULE,
        failed_job_id=_JOB,
    )
    store = CaptureStore(tmp_path / "private", authorization)
    store.prepare()

    class Inner:
        def generate_text(self, *_args: object, **_kwargs: object) -> GeneratedText:
            return GeneratedText(
                "{}",
                ProviderName.OPENAI,
                "gpt-4o-mini-2024-07-18",
                "request",
                1,
                1,
                0,
            )

    wrapper = CaptureStructuredTextGenerator(
        Inner(), store, {ProviderName.OPENAI: "https://api.openai.com/v1/responses"}
    )
    binding = ProviderAttemptBinding(
        UUID(int=7),
        CurationStage.CARD_FAST_CLASSIFY,
        1,
        "shadow",
        lambda _event: None,
        replay_namespace="capture-test",
    )
    with bind_provider_attempts(binding), provider_call_scope(batch_index=0):
        begin_provider_call(
            provider="openai",
            model="gpt-4o-mini",
            instruction="instruction",
            input_text="input",
            output_schema={},
            generation_parameters={
                "thinking": "disabled",
                "thinking_budget_tokens": 1024,
                "temperature": None,
                "max_tokens": 10,
            },
            cacheable_source_prefix=None,
        )
        generated = wrapper.generate_text(
            "instruction",
            "input",
            output_schema={},
            provider=ProviderName.OPENAI,
            model="gpt-4o-mini",
            options=GenerationOptions(max_tokens=10),
        )
    assert generated.model == "gpt-4o-mini"
    records = json.loads((store.pack / "structured.json").read_text(encoding="utf-8"))
    assert {record["model"] for record in records.values()} == {"gpt-4o-mini"}
    replay = ReplayStructuredTextGenerator(
        store.pack / "structured.json", require_attempt_identity=True
    )
    replay_binding = ProviderAttemptBinding(
        UUID(int=8),
        CurationStage.CARD_FAST_CLASSIFY,
        1,
        "canonical",
        lambda _event: None,
        replay_namespace="capture-test",
    )
    with bind_provider_attempts(replay_binding), provider_call_scope(batch_index=0):
        begin_provider_call(
            provider="openai",
            model="gpt-4o-mini",
            instruction="instruction",
            input_text="input",
            output_schema={},
            generation_parameters={
                "thinking": "disabled",
                "thinking_budget_tokens": 1024,
                "temperature": None,
                "max_tokens": 10,
            },
            cacheable_source_prefix=None,
        )
        replayed = replay.generate_text(
            "instruction",
            "input",
            output_schema={},
            provider=ProviderName.OPENAI,
            model="gpt-4o-mini",
            options=GenerationOptions(max_tokens=10),
        )
    assert replayed.model == "gpt-4o-mini"


@pytest.mark.parametrize(
    "returned",
    ("gpt-4o", "gpt-4o-mini-latest", "gpt-4o-mini-2024-13-40"),
)
def test_capture_rejects_unrelated_openai_returned_model(returned: str) -> None:
    assert not capture_module._capture_returned_model_is_authorized(
        ProviderName.OPENAI, "gpt-4o-mini", returned
    )
    assert not capture_module._capture_returned_model_is_authorized(
        ProviderName.ANTHROPIC, "claude-sonnet-5", "claude-sonnet-5-2026-06-30"
    )


@pytest.mark.parametrize(
    ("stage", "kind"),
    (
        (CurationStage.CARD_PREFILTER, "primary"),
        (CurationStage.LCL, "primary"),
        (CurationStage.CARD_EVIDENCE_AUDIT, "primary"),
        (CurationStage.CARD_LEDGER, "embedding"),
    ),
)
def test_capture_structured_generator_rejects_unsanctioned_stage_or_kind(
    tmp_path: Path, stage: CurationStage, kind: str
) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    invoked = False

    class Inner:
        def generate_text(self, *_args: object, **_kwargs: object) -> GeneratedText:
            nonlocal invoked
            invoked = True
            return GeneratedText("{}", ProviderName.OPENROUTER, "test-model", "id", 0, 0, 0)

    generator = CaptureStructuredTextGenerator(
        Inner(), store, {ProviderName.OPENROUTER: "https://openrouter.example/v1/chat"}
    )
    binding = ProviderAttemptBinding(
        UUID(int=31), stage, 1, "shadow", lambda _event: None, replay_namespace="capture-test"
    )
    with bind_provider_attempts(binding), provider_call_scope(batch_index=0, kind=kind):  # type: ignore[arg-type]
        begin_provider_call(
            provider="openrouter",
            model="test-model",
            instruction="instruction",
            input_text="input",
            output_schema={},
            generation_parameters={
                "thinking": "disabled",
                "thinking_budget_tokens": 1024,
                "temperature": None,
                "max_tokens": 1,
            },
            cacheable_source_prefix=None,
        )
        with pytest.raises(CaptureDenied, match="stage or call kind"):
            generator.generate_text(
                "instruction",
                "input",
                output_schema={},
                provider=ProviderName.OPENROUTER,
                model="test-model",
                options=GenerationOptions(max_tokens=1),
            )
    assert invoked is False
    assert store.calls() == []


def test_capture_reconciliation_rejects_lockstep_structured_relocation(
    tmp_path: Path,
) -> None:
    store = CaptureStore(tmp_path / "private", _load(tmp_path / "authorization.json"))
    store.prepare()
    identity = _identity() | {"call_kind": "primary"}
    parameters = {
        "thinking": "disabled",
        "thinking_budget_tokens": 1024,
        "temperature": None,
        "max_tokens": None,
    }
    request = {
        "kind": "structured",
        "provider": "openrouter",
        "model": "test-model",
        "instruction_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "output_schema_sha256": "c" * 64,
        "cache_prefix_sha256": None,
        "generation_parameters": parameters,
        "replay_identity": identity,
    }

    def key_for(value: dict[str, object]) -> str:
        return structured_request_key_from_hashes(
            provider="openrouter",
            model="test-model",
            instruction_sha256="a" * 64,
            input_sha256="b" * 64,
            output_schema_sha256="c" * 64,
            cache_prefix_sha256=None,
            generation_parameters=value["generation_parameters"],  # type: ignore[arg-type]
            attempt_identity=value["replay_identity"],  # type: ignore[arg-type]
        )

    request["key"] = key_for(request)
    ordinal = store.reserve(
        kind="structured",
        provider="openrouter",
        model="test-model",
        request_sha256="a" * 64,
        replay_identity=identity,
        replay_request=request,
    )
    response = GeneratedText(
        '{"ok":true}', ProviderName.OPENROUTER, "test-model", "request", 0, 0, 0
    )
    store.record_structured(str(request["key"]), response)
    digest = capture_module._provider_response_sha256(response.text)
    store.bind_private_response(ordinal, digest, request)
    store.complete(ordinal, observed_microusd=0, stored=True)
    response_event = {
        "response_sha256": digest,
        "request_sha256": "a" * 64,
        "provider": "openrouter",
        "model": "test-model",
        "instruction_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "output_schema_sha256": "c" * 64,
        "cache_prefix_sha256": None,
        "generation_parameters": {**parameters, "max_tokens": 10},
        "request_id": "request",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_microusd": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    rows = [
        {
            "id": index,
            "event": event,
            "provider": "openrouter",
            "model": "test-model",
            "request_sha256": "a" * 64,
            "stage": "card_prefilter",
            "kind": "primary",
            "batch_index": 0,
            "batch_note_ids_sha256": "0" * 64,
            "subcall_ordinal": 0,
            **(response_event if event == "response_received" else {}),
        }
        for index, event in enumerate(("begun", "dispatched", "response_received", "accepted"), 1)
    ]
    harness = ProcessRehearsal(_capture_request(tmp_path))
    harness._capture_store = store
    harness._reconcile_capture_calls(rows)
    original_ledger = json.loads((store.root / "capture-ledger.json").read_text(encoding="utf-8"))
    original_records = json.loads((store.pack / "structured.json").read_text(encoding="utf-8"))
    relocated = _identity() | {"stage": "card_gap_fill", "call_kind": "repair"}
    ledger = json.loads(json.dumps(original_ledger))
    for field in ("replay_identity",):
        ledger["calls"][0][field] = relocated
    for field in ("replay_request", "private_response"):
        ledger["calls"][0][field]["replay_identity"] = relocated
        ledger["calls"][0][field]["key"] = key_for(ledger["calls"][0][field])
    new_key = ledger["calls"][0]["replay_request"]["key"]
    records = {new_key: original_records[str(request["key"])]}
    store._write_json(store.root / "capture-ledger.json", ledger)  # noqa: SLF001
    store._write_json(store.pack / "structured.json", records)  # noqa: SLF001
    with pytest.raises(RuntimeError, match="exactly one provider dispatch"):
        harness._reconcile_capture_calls(rows)
    store._write_json(store.root / "capture-ledger.json", original_ledger)  # noqa: SLF001
    store._write_json(store.pack / "structured.json", original_records)  # noqa: SLF001
    ledger = json.loads(json.dumps(original_ledger))
    for field in ("replay_request", "private_response"):
        ledger["calls"][0][field]["generation_parameters"]["max_tokens"] = 1
        ledger["calls"][0][field]["key"] = key_for(ledger["calls"][0][field])
    new_key = ledger["calls"][0]["replay_request"]["key"]
    records = {new_key: original_records[str(request["key"])]}
    store._write_json(store.root / "capture-ledger.json", ledger)  # noqa: SLF001
    store._write_json(store.pack / "structured.json", records)  # noqa: SLF001
    with pytest.raises(RuntimeError, match="private response"):
        harness._reconcile_capture_calls(rows)


def test_replay_completion_rejects_invalid_ids_and_reparse_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supplement = tmp_path / "supplement"
    manifest_sha256 = _write_replay_supplement(supplement, populated=True)
    completion = tmp_path / "completion.json"
    expected = _write_completion(completion, capsule=_CAPSULE, pack_manifest="d" * 64)
    value = json.loads(completion.read_text(encoding="utf-8"))
    value["job_id"] = "not-a-uuid"
    completion.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid job identities"):
        _verify_replay_completion(
            completion,
            hashlib.sha256(completion.read_bytes()).hexdigest(),
            supplement_root=supplement,
            expected_manifest_sha256=manifest_sha256,
            expected_commit=_COMMIT,
            expected_tree=_TREE,
            expected_capsule_sha256=_CAPSULE,
            expected_pack_sha256="d" * 64,
        )
    _write_completion(completion, capsule=_CAPSULE, pack_manifest="d" * 64)
    monkeypatch.setattr(
        process_module, "_is_indirect", lambda path: path == completion.absolute()
    )
    with pytest.raises(ValueError, match="unavailable"):
        _verify_replay_completion(
            completion,
            expected,
            supplement_root=supplement,
            expected_manifest_sha256=manifest_sha256,
            expected_commit=_COMMIT,
            expected_tree=_TREE,
            expected_capsule_sha256=_CAPSULE,
            expected_pack_sha256="d" * 64,
        )
