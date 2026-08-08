import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from oms_anki_agent.hub_client import (
    HubAuthenticationError,
    HubClient,
    HubProtocolError,
    HubUnavailable,
)
from oms_anki_agent.snapshot import PreparedSnapshot
from oms_hub.anki.contracts import (
    AgentHeartbeat,
    SnapshotDelta,
    SnapshotManifest,
    SnapshotNote,
)

TOKEN = "sentinel-hub-token"


class MemorySecrets:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get(self, key: str) -> str | None:
        assert key == "anki-agent-token"
        return self.value

    def set(self, key: str, value: str) -> None:
        self.value = value

    def delete(self, key: str) -> None:
        self.value = None


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    token: str | None = TOKEN,
) -> HubClient:
    return HubClient(
        hub_url="https://study-hub.tailnet-name.ts.net",
        agent_id="connor-mac",
        token_key="anki-agent-token",
        secrets=MemorySecrets(token),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_hub_client_reads_bearer_for_each_request_without_serializing_it() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["x-oms-agent-id"] == "connor-mac"
        return httpx.Response(200, json={"status": "ok"})

    client = _client(handler)

    assert client.health() == {"status": "ok"}
    assert TOKEN not in repr(client)
    assert len(requests) == 1


def test_hub_client_posts_strict_heartbeat() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/agent/v1/heartbeat"
        payload = json.loads(request.content)
        assert payload["agent_id"] == "connor-mac"
        assert payload["contract_version"] == 1
        return httpx.Response(200, json={"status": "ok"})

    heartbeat = AgentHeartbeat(
        agent_id="connor-mac",
        agent_version="0.1.0",
        anki_version="25.02",
        ankiconnect_version=6,
        active_snapshot_id=None,
        health="ok",
        observed_at="2026-07-27T15:00:00Z",
    )

    assert _client(handler).post_heartbeat(heartbeat) == {"status": "ok"}


def test_hub_client_fails_closed_when_keychain_token_is_missing() -> None:
    client = _client(lambda request: httpx.Response(200), token=None)

    with pytest.raises(HubAuthenticationError, match="Keychain"):
        client.health()


def test_hub_client_classifies_network_and_service_failures_as_transient() -> None:
    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(HubUnavailable) as network:
        _client(network_failure).health()
    assert network.value.transient is True

    with pytest.raises(HubUnavailable) as service:
        _client(lambda request: httpx.Response(503)).health()
    assert service.value.transient is True


def test_hub_client_rejects_authentication_and_malformed_payloads() -> None:
    with pytest.raises(HubAuthenticationError):
        _client(lambda request: httpx.Response(401)).health()
    with pytest.raises(HubProtocolError):
        _client(lambda request: httpx.Response(200, text="not-json")).health()


def _snapshot() -> SnapshotDelta:
    manifest = SnapshotManifest(
        snapshot_id="full-a-b",
        source_deck="Anking Step Deck",
        note_count=1,
        id_set_sha256="a" * 64,
        content_sha256="b" * 64,
        export_version="1",
        agent_version="test",
        ankiconnect_version=6,
        exported_at=datetime(2026, 7, 27, tzinfo=UTC),
        payload_sha256="c" * 64,
    )
    note = SnapshotNote(
        note_id=101,
        model_name="AnKingOverhaul",
        fields={"Text": "anemia"},
        tags=(),
        card_ids=(1001,),
        media=(),
        content_sha256="d" * 64,
    )
    return SnapshotDelta(
        manifest=manifest,
        upserts=(note,),
        deleted_note_ids=(),
        payload_sha256="e" * 64,
    )


def test_hub_client_polls_zero_or_one_strict_command() -> None:
    no_command = _client(lambda request: httpx.Response(204))
    assert no_command.next_command() is None

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/agent/v1/commands/next"
        return httpx.Response(
            200,
            json={
                "contract_version": 1,
                "command_id": "b2edb9da-4421-4d27-bc6b-7797ed310355",
                "command_type": "full_snapshot",
                "payload": {},
                "payload_sha256": "a" * 64,
                "created_at": "2026-07-27T12:00:00Z",
            },
        )

    command = _client(handler).next_command()
    assert command is not None
    assert command.command_type == "full_snapshot"


def test_hub_client_uploads_snapshot_to_owned_command() -> None:
    command_id = UUID("b2edb9da-4421-4d27-bc6b-7797ed310355")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/agent/v1/commands/{command_id}/snapshot"
        assert json.loads(request.content)["payload_sha256"] == "e" * 64
        return httpx.Response(200, json={"status": "accepted"})

    assert _client(handler).upload_snapshot(command_id, _snapshot()) == {
        "status": "accepted"
    }


def test_hub_client_streams_prepared_snapshot_with_content_length(
    tmp_path: Path,
) -> None:
    command_id = UUID("b2edb9da-4421-4d27-bc6b-7797ed310355")
    payload_path = tmp_path / "snapshot.json"
    payload_path.write_text(_snapshot().model_dump_json(), encoding="utf-8")
    prepared = PreparedSnapshot(
        manifest=_snapshot().manifest,
        payload_sha256=_snapshot().payload_sha256,
        payload_path=payload_path,
        note_hashes={101: "d" * 64},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert int(request.headers["content-length"]) == len(body)
        assert json.loads(body)["payload_sha256"] == "e" * 64
        return httpx.Response(200, json={"status": "accepted"})

    assert _client(handler).upload_snapshot(command_id, prepared) == {
        "status": "accepted"
    }
