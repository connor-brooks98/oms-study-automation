import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

import numpy as np
import pytest

from oms_hub.anki.domain import CurationStage
from oms_hub.anki.index import AnkiIndex
from oms_hub.anki.normalize import NormalizedNote
from oms_hub.anki.provider_attempts import (
    ProviderAttemptBinding,
    ProviderAttemptLifecycle,
    bind_provider_attempts,
    provider_call_scope,
)
from oms_hub.anki.rehearsal.network import (
    EgressDenied,
    EgressEvidenceLedger,
    EgressPolicy,
    SocketEgressGuard,
)
from oms_hub.anki.rehearsal.runtime import ReadOnlyAnkiGateway, RehearsalMutationDenied
from oms_hub.anki.rehearsal.structured import (
    ReplayStructuredTextGenerator,
    StructuredReplayMiss,
    structured_request_key,
)
from oms_hub.anki.rehearsal.vectors import ReplayEmbeddingClient, ReplayVectorMiss
from oms_hub.llm.domain import GenerationOptions, ProviderName


class _FixedEmbedder:
    model_name = "fixed"

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)


def test_read_only_gateway_serves_snapshot_and_rejects_mutation(tmp_path: Path) -> None:
    index = AnkiIndex(tmp_path / "companion", embedder=_FixedEmbedder())
    index.rebuild(
        [
            NormalizedNote(
                note_id=123,
                model_name="Cloze",
                text="alpha",
                extra="beta",
                raw_fields={"Text": "alpha", "Extra": "beta"},
                tags=("heme",),
                card_ids=(456,),
                media=(),
                token_signature="alpha beta",
                content_sha256="a" * 64,
            )
        ],
        snapshot_id="snapshot-1",
        fingerprint="f" * 64,
    )
    evidence = tmp_path / "runtime-evidence"
    gateway = ReadOnlyAnkiGateway(
        index,
        profile="A0 Rehearsal",
        evidence_directory=evidence,
        run_nonce="run-nonce",
    )
    assert asyncio.run(gateway.version()) == 6
    assert asyncio.run(gateway.get_active_profile()) == "A0 Rehearsal"
    assert asyncio.run(gateway.find_notes("")) == [123]
    info = asyncio.run(gateway.notes_info([123]))
    assert info[0]["fields"]["Text"]["value"] == "alpha"
    with pytest.raises(RehearsalMutationDenied):
        asyncio.run(gateway.add_tags([123], ["forbidden"]))
    with pytest.raises(RehearsalMutationDenied):
        asyncio.run(gateway.sync())
    asyncio.run(gateway.aclose())
    ledger = json.loads((evidence / "read-only-anki-mutation-ledger.json").read_text())
    assert ledger["run_nonce"] == "run-nonce"
    assert [record["action"] for record in ledger["records"]] == ["addTags", "sync"]
    assert all(
        set(record) == {"action", "ordinal", "timestamp", "outcome"} for record in ledger["records"]
    )


def test_empty_read_only_ledger_is_valid_close_evidence(tmp_path: Path) -> None:
    index = AnkiIndex(tmp_path / "companion", embedder=_FixedEmbedder())
    gateway = ReadOnlyAnkiGateway(
        index,
        evidence_directory=tmp_path / "runtime-evidence",
        run_nonce="run-nonce",
    )
    asyncio.run(gateway.aclose())
    ledger = json.loads(
        (tmp_path / "runtime-evidence/read-only-anki-mutation-ledger.json").read_text()
    )
    assert ledger["records"] == []


def test_read_only_ledger_rejects_a_stale_nonce(tmp_path: Path) -> None:
    evidence = tmp_path / "runtime-evidence"
    evidence.mkdir()
    (evidence / "read-only-anki-mutation-ledger.json").write_text(
        json.dumps({"schema_version": 1, "run_nonce": "old", "records": []}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="stale or malformed"):
        ReadOnlyAnkiGateway(
            AnkiIndex(tmp_path / "companion", embedder=_FixedEmbedder()),
            evidence_directory=evidence,
            run_nonce="new",
        )


def test_replay_embeddings_fail_closed_and_count_document_hits(tmp_path: Path) -> None:
    store = tmp_path / "vectors"
    client = ReplayEmbeddingClient(store, model="voyage-4-large", dimensions=2)
    client.seed(["alpha"], input_type="document", vectors=np.array([[1, 0]], dtype=np.float32))
    vectors = asyncio.run(client.embed(["alpha"], input_type="document"))
    assert vectors.tolist() == [[1.0, 0.0]]
    assert client.evidence.document_replay_hits == 1
    assert client.evidence.live_document_calls == 0
    with pytest.raises(ReplayVectorMiss):
        asyncio.run(client.embed(["beta"], input_type="query"))


def _replay_attempt_events(client: ReplayEmbeddingClient) -> ProviderAttemptLifecycle:
    lifecycle = ProviderAttemptLifecycle()
    binding = ProviderAttemptBinding(
        job_id=UUID(int=7),
        stage=CurationStage.CARD_FAST_CLASSIFY,
        stage_attempt=1,
        mode="canonical",
        recorder=lambda evidence: lifecycle.append(evidence.event),
    )
    with bind_provider_attempts(binding), provider_call_scope(batch_index=0, kind="embedding"):
        with pytest.raises(ReplayVectorMiss, match="replay vector validation failed"):
            asyncio.run(client.embed(["alpha"], input_type="document"))
    assert [event.event for event in lifecycle.events] == [
        "begun",
        "dispatched",
        "validation_failed",
    ]
    assert lifecycle.events[-1].error == "replay vector validation failed"
    lifecycle.require_safe_to_retry()
    return lifecycle


@pytest.mark.parametrize(
    "corruption",
    (
        "missing_manifest",
        "missing_file",
        "malformed_json",
        "malformed_record",
        "traversal",
        "symlink_escape",
        "corrupt_numpy",
        "hash_mismatch",
    ),
)
def test_replay_embedding_invalid_local_evidence_is_terminal_and_retry_safe(
    tmp_path: Path, corruption: str
) -> None:
    store = tmp_path / "vectors"
    client = ReplayEmbeddingClient(store, model="voyage-4-large", dimensions=2)
    client.seed(["alpha"], input_type="document", vectors=np.array([[1, 0]], dtype=np.float32))
    manifest_path = store / "manifest.json"
    vector_path = next((store / "document").glob("*.npy"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    key = next(iter(manifest))

    if corruption == "missing_manifest":
        manifest_path.unlink()
    elif corruption == "missing_file":
        vector_path.unlink()
    elif corruption == "malformed_json":
        manifest_path.write_text("{not json", encoding="utf-8")
    elif corruption == "malformed_record":
        manifest[key] = ["not", "a", "record"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif corruption == "traversal":
        manifest[key]["path"] = "../outside.npy"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif corruption == "symlink_escape":
        external = tmp_path / "outside.npy"
        np.save(external, np.array([1, 0], dtype=np.float32), allow_pickle=False)
        vector_path.unlink()
        try:
            vector_path.symlink_to(external)
        except OSError as exc:
            pytest.skip(f"symbolic links unavailable: {exc}")
    elif corruption == "corrupt_numpy":
        vector_path.write_bytes(b"not a numpy array")
        manifest[key]["sha256"] = hashlib.sha256(vector_path.read_bytes()).hexdigest()
        manifest[key]["size_bytes"] = vector_path.stat().st_size
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif corruption == "hash_mismatch":
        vector_path.write_bytes(vector_path.read_bytes() + b"corrupt")
    else:
        raise AssertionError(f"unknown corruption fixture: {corruption}")

    first = _replay_attempt_events(client)
    second = _replay_attempt_events(client)
    assert first.terminal and second.terminal
    assert client.evidence.replay_misses == 2


def test_deterministic_egress_policy_allows_loopback_only() -> None:
    policy = EgressPolicy.deterministic()
    policy.authorize("127.0.0.1", 8766)
    policy.authorize("localhost", 8787)
    with pytest.raises(EgressDenied):
        policy.authorize("api.voyageai.com", 443)


def test_shadow_egress_policy_requires_pinned_host_and_address() -> None:
    policy = EgressPolicy.shadow({"api.anthropic.com": {"203.0.113.8"}})
    policy.authorize("api.anthropic.com", 443, resolved_address="203.0.113.8")
    with pytest.raises(EgressDenied):
        policy.authorize("api.anthropic.com", 8443, resolved_address="203.0.113.8")
    with pytest.raises(EgressDenied):
        policy.authorize("api.anthropic.com", 443, resolved_address="203.0.113.9")
    with pytest.raises(EgressDenied):
        policy.authorize("example.com", 443, resolved_address="203.0.113.8")


@pytest.mark.parametrize(
    "host",
    (
        "203.0.113.8",
        "001.002.003.004",
        "2130706433",
        "0x7f000001",
        "0x7f.0.0.1",
        "0x7f.1",
        "0177.0.0.1",
        "[2001:db8::8]",
        "2001:0db8:0:0:0:0:0:8",
        "::1",
    ),
)
def test_shadow_egress_rejects_ip_literals_as_pin_keys(host: str) -> None:
    with pytest.raises(ValueError, match="DNS hostnames"):
        EgressPolicy.shadow({host: {"203.0.113.8"}})


def test_shadow_egress_resolution_never_globally_authorizes_a_direct_ip() -> None:
    policy = EgressPolicy.shadow({"api.anthropic.com": {"203.0.113.8"}})
    with pytest.raises(EgressDenied):
        policy.authorize_connect("203.0.113.8", 443, ("203.0.113.8", 443))
    rows = policy.resolve("api.anthropic.com", 443)
    assert rows[0][-1] == ("203.0.113.8", 443)
    with pytest.raises(EgressDenied):
        policy.authorize_connect("203.0.113.8", 443, ("203.0.113.8", 443))


def test_shadow_socket_guard_consumes_resolution_tokens_and_blocks_later_direct_ip() -> None:
    import socket

    guard = SocketEgressGuard(EgressPolicy.shadow({"api.anthropic.com": {"203.0.113.8"}}))
    observed: list[tuple[object, ...]] = []

    def connect(sock: socket.socket, address: tuple[object, ...]) -> None:
        del sock
        observed.append(address)

    guard._original_connect = connect  # type: ignore[assignment]
    guard.install()
    try:
        rows = socket.getaddrinfo("api.anthropic.com", 443, type=socket.SOCK_STREAM)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect(rows[0][-1])
        assert observed == [("203.0.113.8", 443)]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with pytest.raises(EgressDenied):
                sock.connect(("203.0.113.8", 443))
    finally:
        guard.uninstall()


def test_process_socket_guard_denies_external_name_resolution() -> None:
    import socket

    guard = SocketEgressGuard(EgressPolicy.deterministic())
    guard.install()
    try:
        assert socket.getaddrinfo("localhost", 8787)
        with pytest.raises(EgressDenied):
            socket.getaddrinfo("api.voyageai.com", 443)
    finally:
        guard.uninstall()


def test_process_socket_guard_blocks_udp_and_non_tcp_resolution() -> None:
    import socket

    guard = SocketEgressGuard(EgressPolicy.deterministic())
    guard.install()
    try:
        with pytest.raises(EgressDenied):
            socket.getaddrinfo("127.0.0.1", 8787, type=socket.SOCK_DGRAM)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            with pytest.raises(EgressDenied):
                sock.sendto(b"blocked", ("127.0.0.1", 8787))
    finally:
        guard.uninstall()


def test_egress_ledger_records_lifecycle_and_denial_before_raising(tmp_path: Path) -> None:
    ledger = EgressEvidenceLedger(
        tmp_path / "runtime-evidence", mode="deterministic", run_nonce="run-nonce"
    )
    guard = SocketEgressGuard(EgressPolicy.deterministic(ledger))
    guard.install()
    try:
        with pytest.raises(EgressDenied):
            guard.policy.authorize("api.voyageai.com", 443)
    finally:
        guard.uninstall()
    evidence = json.loads((tmp_path / "runtime-evidence/egress-decisions.json").read_text())
    assert evidence["run_nonce"] == "run-nonce"
    assert [record["kind"] for record in evidence["records"]] == [
        "startup",
        "authorization",
        "shutdown",
    ]
    assert evidence["records"][1]["allowed"] is False


def test_egress_ledger_accumulates_across_rehearsal_restarts(tmp_path: Path) -> None:
    directory = tmp_path / "runtime-evidence"
    first = EgressPolicy.deterministic(
        EgressEvidenceLedger(directory, mode="deterministic", run_nonce="run-nonce")
    )
    first.record_startup()
    first.record_shutdown()
    second = EgressPolicy.deterministic(
        EgressEvidenceLedger(directory, mode="deterministic", run_nonce="run-nonce")
    )
    second.record_startup()
    second.record_shutdown()
    evidence = json.loads((directory / "egress-decisions.json").read_text())
    assert [record["kind"] for record in evidence["records"]] == [
        "startup",
        "shutdown",
        "startup",
        "shutdown",
    ]
    assert [record["ordinal"] for record in evidence["records"]] == [1, 2, 3, 4]


def test_structured_replay_is_content_addressed_and_has_no_live_fallback(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "structured.json"
    schema: dict[str, object] = {"type": "object"}
    options = GenerationOptions(temperature=0)
    key = structured_request_key(
        "instruction",
        "input",
        output_schema=schema,
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        options=options,
    )
    response = '{"value":1}'
    manifest.write_text(
        json.dumps(
            {
                key: {
                    "text": response,
                    "text_sha256": hashlib.sha256(response.encode()).hexdigest(),
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "request_id": "replay-1",
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cost_microusd": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    generator = ReplayStructuredTextGenerator(manifest)
    generated = generator.generate_text(
        "instruction",
        "input",
        output_schema=schema,
        provider=ProviderName.ANTHROPIC,
        model="claude-sonnet-5",
        options=options,
    )
    assert generated.text == response
    assert generator.evidence.hits == 1
    assert generator.evidence.live_calls == 0
    with pytest.raises(StructuredReplayMiss):
        generator.generate_text(
            "instruction",
            "changed input",
            output_schema=schema,
            provider=ProviderName.ANTHROPIC,
            model="claude-sonnet-5",
            options=options,
        )


def test_rehearsal_structured_replay_requires_and_binds_durable_attempt_identity(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "structured.json"
    generator = ReplayStructuredTextGenerator(manifest, require_attempt_identity=True)
    with pytest.raises(StructuredReplayMiss, match="durable attempt identity"):
        generator.generate_text(
            "instruction",
            "input",
            output_schema={"type": "object"},
            provider=ProviderName.OPENAI,
            model="model",
        )
    events: list[object] = []
    binding = ProviderAttemptBinding(
        job_id=UUID(int=1),
        stage=CurationStage.CARD_GAP_FILL,
        stage_attempt=2,
        mode="shadow",
        recorder=events.append,
    )
    # The key cannot be obtained from a bare provider request: a durable call
    # handle is allocated before the deterministic generator sees the request.
    from oms_hub.anki.provider_attempts import begin_provider_call

    with bind_provider_attempts(binding), provider_call_scope(batch_index=0):
        handle = begin_provider_call(
            provider="openai",
            model="model",
            instruction="instruction",
            input_text="input",
            output_schema={"type": "object"},
            generation_parameters={
                "thinking": "disabled",
                "thinking_budget_tokens": 1024,
                "temperature": None,
                "max_tokens": None,
            },
            cacheable_source_prefix=None,
        )
        key = structured_request_key(
            "instruction",
            "input",
            output_schema={"type": "object"},
            provider=ProviderName.OPENAI,
            model="model",
            options=GenerationOptions(),
            attempt_identity=handle.identity,
        )
        manifest.write_text(
            json.dumps(
                {
                    key: {
                        "text": "{}",
                        "text_sha256": hashlib.sha256(b"{}").hexdigest(),
                        "provider": "openai",
                        "model": "model",
                        "request_id": "request",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_microusd": 0,
                    }
                }
            ),
            encoding="utf-8",
        )
        assert generator.generate_text(
            "instruction",
            "input",
            output_schema={"type": "object"},
            provider=ProviderName.OPENAI,
            model="model",
        ).request_id == "request"
