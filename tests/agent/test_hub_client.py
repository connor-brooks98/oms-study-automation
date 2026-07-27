import json
from collections.abc import Callable

import httpx
import pytest

from oms_anki_agent.hub_client import (
    HubAuthenticationError,
    HubClient,
    HubProtocolError,
    HubUnavailable,
)
from oms_hub.anki.contracts import AgentHeartbeat

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
